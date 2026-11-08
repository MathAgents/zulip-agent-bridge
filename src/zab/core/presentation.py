"""Core-owned card/status formatting with safe text boundaries (SPEC §4.9)."""

from __future__ import annotations

import re
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from zab.core.types import InvalidSetting, ItemInfo, ItemKind, Renderer
from zab.render.links import ZULIP_TIME
from zab.render.markdown import normalize, redact_secrets


def code(text: str) -> str:
    # Redacted, and again once on one line, which can make a key's context whole: `curl -u`
    # and a newline before the user, `ZULIP_API_KEY` and one before the `=` (SPEC §11.6, #666).
    text = redact_secrets(redact_secrets(text).replace("\n", " "))
    width = max((len(x) for x in re.findall(r"`+", text)), default=0) + 1
    fence = "`" * width
    return f"{fence} {text} {fence}"


AGENT_NAMES = {"claude": "Claude", "codex": "Codex"}


def bridge_note(kind: str, text: str, level: str = "warning") -> str:
    """`text` marked as the bridge's own words, not the agent's (SPEC §4.9, B.4; issue #677):
    🔧 for information, ⚠️ for warnings and errors. `kind` is the bot's backend kind."""
    icon = "🔧" if level == "info" else "⚠️"
    return f"{icon} **Bridge** (not {AGENT_NAMES.get(kind, 'the agent')}): {text}"


def invalid_setting(error: InvalidSetting) -> str:
    name = error.field if error.field in ("model", "effort") else "setting"
    choices = ", ".join(error.valid) or "none reported"
    return f"Unknown {name} {code(error.value or 'default')}. Valid: {code(choices)}" + (
        ". " + code(error.message) if error.message else ""
    )


def block(text: str) -> str:
    text = redact_secrets(text)
    width = max(3, max((len(x) for x in re.findall(r"`+", text)), default=0) + 1)
    fence = "`" * width
    return f"{fence}\n{text}\n{fence}"


def card(row: dict[str, Any], machine: str, resume: str, turns: int) -> str:
    return (
        "**zab session** · 🤖 Session started. Write in this topic to continue (mention "
        "me if others are in the channel); !help lists commands, !session shows details."
    )


#: The states of a turn that ended: its status message's final edit (`EditBounds`, #754).
FINISHED = ("completed", "interrupted", "failed")
#: The agent's notes the status message shows while a turn runs, newest last (issue #654).
NOTES = 3
#: The longest a note may be on the status message; a longer one is cut after a word (#654).
NOTE_CHARS = 500


def _before_time(text: str, cut: int) -> int:
    """`cut`, or the start of the Zulip time (`links.ZULIP_TIME`) it would fall inside."""
    for m in ZULIP_TIME.finditer(text, 0, cut + 107):  # a time is at most 107 characters
        if m.start() >= cut:
            break
        if cut < m.end():
            return m.start()
    return cut


def clip(text: str, limit: int = NOTE_CHARS) -> str:
    """`text` on one line, at most `limit` characters: cut after a whole word, then `…`. Only a
    single word longer than that is cut inside it (issue #654). A Zulip time, `<time:…>`, is
    never cut: a cut inside one goes before it, since half of one shows as raw text (#673),
    and a time counts as one word, so a note that would be left empty is cut inside the word
    after it instead. Redact before: one line defeats the line-anchored rules of
    `redact_secrets` (SPEC §11.6)."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    space = text.rfind(" ", 0, limit)
    cut = _before_time(text, space if space > 0 else limit - 1)
    if cut == 0:  # the only space was inside a time at the start
        cut = _before_time(text, limit - 1)
    return text[:cut].rstrip() + "…"


def status(
    state: str,
    started: float,
    actions: list[ItemInfo],
    renderer: Renderer,
    limit: int,
    plan: tuple = (),
    notes: Sequence[str] = (),
    *,
    model: str | None = None,
    effort: str | None = None,
    effort_supported: bool | None = None,
    ended: float | None = None,
) -> str:
    """The status message (SPEC §4.9, B.2). `notes` are the agent's interim texts, oldest
    first. Nothing in it changes only because time passes (#754): while the turn runs it shows
    when it started, as a Zulip time, and once it ended, how long it took, until `ended`."""
    heading = {
        "running": "⏳ **Working**",
        "awaiting_approval": "⏸ **Waiting for approval**",
        "completed": "✅ **Done**",
        "interrupted": "⏹ **Stopped**",
        "failed": "❌ **Failed**",
        "stopping": "⏹ stopping…",
        "error": "⚠️ **Error**",
    }.get(state) or f"**{state[:1].upper()}{state[1:]}**"  # e.g. "backend reconnecting"
    finished = state in FINISHED
    if finished:
        elapsed = max(0, int((ended or time.time()) - started))
        when = f"{elapsed // 60}m {elapsed % 60:02d}s"
    else:
        when = "started <time:" + datetime.fromtimestamp(int(started), UTC).isoformat() + ">"
    lines = [f"{heading} · {when} · {len(actions)} actions"]
    lines[0] += " · " + (code(model) if model else "model default")
    if effort_supported is not False:
        lines[0] += " · effort " + (code(effort) if effort else "default")
    selected = actions[-50:] if finished else actions[-8:]
    detail = []
    for item in selected:
        title = (
            code(item.title)
            if item.kind in (ItemKind.COMMAND, ItemKind.FILE_CHANGE)
            else renderer.render_inline(item.title)
        )
        icon = {"in_progress": "⏳", "failed": "❌", "declined": "⛔"}.get(item.status, "✅")
        detail.append(f"- {icon} {title}")
    if plan and not finished:
        glyph = {"pending": "☐", "in_progress": "◐", "completed": "☑"}
        detail.append(
            "Plan: " + " · ".join(f"{glyph[s]} {renderer.render_inline(p)}" for p, s in plan)
        )
    if isinstance(notes, str):
        notes = [notes]  # one note, as callers passed it before #654
    if not finished:
        # Redacted, NULs first as `render_inline` does, before `clip` puts it on one line: the
        # zuliprc rule matches whole lines (SPEC §11.6; review of #658).
        notes = [clip(redact_secrets(normalize(n))) for n in notes if n.strip()][-NOTES:]
        # One quote, a paragraph per note: the `code` or **bold** a cut left open in one note
        # cannot pair with the next note's.
        detail += [
            (">\n> " if i else "> ") + renderer.render_inline(note) for i, note in enumerate(notes)
        ]
    budget = max(0, limit - 200 - len(lines[0]) - 100)
    # Drop whole lines so code spans/mention neutralization cannot be cut open.
    while detail and len("\n".join(detail)) > budget:
        detail.pop(0)
    if detail and detail[0].startswith(">\n"):
        detail[0] = detail[0][2:]  # the first note went: no empty quote line on top
    body = "\n".join(detail)
    if body:
        lines.append("````spoiler Actions\n" + body + "\n````" if finished else body)
    return "\n".join(lines)
