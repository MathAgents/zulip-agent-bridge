"""On-demand session details; the anchoring card is immutable (SPEC §7, B.1)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from zab.core import presentation as view
from zab.core import types as t
from zab.core.settings import display_setting, effective_settings
from zab.core.usage import number

if TYPE_CHECKING:
    from zab.core.session import SessionActor


def session_details(actor: SessionActor) -> str:
    row = actor.row
    overrides = t.SessionSettings(**json.loads(row["settings_json"]))
    effective = effective_settings(actor.backend, row["native_id"])
    fields = []
    for name in ("model", "effort", "approval", "sandbox", "account"):
        explicit = getattr(overrides, name)
        resolved = getattr(effective, name) if effective else None
        value = resolved or explicit
        if name in ("model", "effort"):
            value = display_setting(name, overrides, effective)
        if name in ("approval", "sandbox"):
            value = value or "default"
        if value:
            fields.append(f"{name}: {view.code(value)}")
    turns = actor.store.rows("turns", session_id=row["id"])
    usage = actor.store.get(f"session_tokens:{row['id']}")
    if actor.backend.kind == "claude" or usage is None:
        counts = [actor.store.get(f"turn_tokens:{turn['id']}") for turn in turns]
        known = [value for value in counts if value is not None]
        usage = sum(known) if known else None
        qualifier = " recorded" if len(known) < len(turns) else ""
    else:
        qualifier = ""
    tokens = number(usage) + qualifier if usage is not None else "unknown"
    return (
        f"**Session details** · {view.code(row['backend'])} · {view.code(actor.machine)}\n"
        f"Session: {view.code(row['native_id'])}\n"
        f"Workspace: {view.code(row['workspace'])} ({row['workspace_mode']}) · "
        f"branch {view.code(row.get('branch') or 'detached')}\n"
        + " · ".join(fields)
        + f"\nState: {row['state']} · {len(turns)} turns · {tokens} tokens\n"
        + f"For SSH users — resume in a terminal: {view.code(actor.resume)}"
    )
