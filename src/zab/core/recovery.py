"""Reconcile persisted turns with backend summaries (SPEC §7.8.1)."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import TYPE_CHECKING

from zab.core import types as t

if TYPE_CHECKING:
    from zab.core.session import SessionActor


def session_spec(actor: SessionActor) -> t.SessionSpec:
    from zab.core.creation import context_prompt

    settings = t.SessionSettings(**json.loads(actor.row["settings_json"]))
    if actor.backend.kind == "codex":
        if actor.turn and json.loads(actor.turn["trigger_msg_ids"]):
            settings.extra["owned_turn_ids"] = [actor.turn["native_turn_id"]]
        settings.extra["client_ids"] = actor.store.get(f"client_ids:{actor.row['id']}", [])
    else:
        # Remove keys persisted by versions that added Codex metadata to Claude cards.
        if {"owned_turn_ids", "client_ids"} & settings.extra.keys():
            settings.extra.pop("owned_turn_ids", None)
            settings.extra.pop("client_ids", None)
            actor.state(settings_json=json.dumps(asdict(settings)))
    return t.SessionSpec(
        cwd=actor.row["workspace"],
        name=actor.row["topic"].removeprefix("✔ "),
        settings=settings,
        git_identity=getattr(actor, "git_identity", None),
        bot=actor.row["bot"],  # ZAB_BOT, for `zab history` (issue #248)
        session=actor.row["id"],  # ZAB_SESSION, for `zab job` (issue #655)
        context_prompt=context_prompt(getattr(actor, "multi_party_enabled", False)),
    )


async def recover(actor: SessionActor, info: t.SessionInfo) -> None:
    """Apply the backend's snapshot (`_reconcile`), then start the `!fork`s that waited for this
    session to reopen (§7.11, #202), on every path out of it that leaves the session ready. That
    includes its early returns, on a reopen inside the process: an answer still undelivered (as
    when a card comes back to a private channel), and a Claude turn still running as this
    bridge's child though the snapshot no longer reports it. Their forks waited for the next
    `BackendStatus(up)` before (#591). Each then runs as a new `!fork` would. Not after a
    failure, which raises (`!status` reopens the session, and starts them), nor for a detached
    session."""
    await _reconcile(actor, info)
    if getattr(actor, "reopened", None) and actor.ready and actor.row["state"] != "detached":
        actor.reopened()  # BotBridge.pending_forks_soon: a background run, never awaited here


async def _reconcile(actor: SessionActor, info: t.SessionInfo) -> None:
    actor.state(settings_json=json.dumps(asdict(info.settings)))
    actor.ready = True
    actor.resume = info.resume_hint
    if actor.completion:
        await actor.complete(actor.completion, drain=False)
        if actor.completion:
            return
    if actor.turn:
        tid = actor.turn["native_turn_id"]
        if info.last_turn and info.last_turn.turn_id == tid:
            summary = info.last_turn
            await actor.complete(
                t.TurnCompleted(
                    session_id=info.native_id,
                    turn_id=tid,
                    status=summary.status,
                    final_text=summary.final_text,
                    error=summary.error,
                ),
                drain=False,
            )
        elif info.active_turn_id == tid:
            actor.state(state="running")
            await actor.reserve_slot()
            await actor.status()
        else:
            claude = actor.backend.kind == "claude"
            if claude and actor.turn.get("pgid"):
                from zab.core.processes import is_bridge_child, reap_claude_turn

                if is_bridge_child(actor.turn["pgid"]):
                    actor.state(state="running")
                    await actor.reserve_slot()
                    await actor.status()
                    await actor.card()
                    return

                launch_id, separator, _ = tid.partition(":")
                await reap_claude_turn(
                    actor.turn["pgid"],
                    launch_id if separator else info.native_id,
                    actor.backend.config.binary,
                )
            if claude:
                actor.store.set(
                    f"restart_notice:{actor.turn['id']}",
                    "⏹ interrupted by bridge restart; send a message to continue",
                )
            await actor.complete(
                t.TurnCompleted(
                    session_id=info.native_id,
                    turn_id=tid,
                    status=t.TurnStatus.INTERRUPTED,
                    final_text="⏹ interrupted by bridge restart; send a message to continue"
                    if claude
                    else actor.note("The stored turn is no longer active."),  # #677
                ),
                drain=False,
            )
    if actor.row["state"] == "detached":
        # Settle only the turn this binding owned; do not adopt a later terminal turn.
        if not actor.turn:
            actor.release()
        return
    if info.active_turn_id and not actor.turn:
        previous = actor.store.rows(
            "turns", session_id=actor.row["id"], native_turn_id=info.active_turn_id
        )
        if previous:
            # A stop timeout is not proof the backend stopped. Reuse its row.
            actor.turn = previous[0]
            actor.store.update(
                "turns",
                {"id": actor.turn["id"]},
                state="running",
                ended_at=None,
                final_message_ids=None,
            )
            actor.turn.update(state="running", ended_at=None, final_message_ids=None)
            actor.state(state="running")
            await actor.reserve_slot()
            await actor.status()
        else:
            await actor.event(t.TurnStarted(session_id=info.native_id, turn_id=info.active_turn_id))
            await actor.reserve_slot()
    elif not actor.turn:
        if not actor.waiter:
            # A waiter's slot belongs to the "begin" it queued, which uses or releases it.
            actor.release()
        actor.unconfirmed_turn = None
        actor.state(state="idle")
        actor.schedule()
    await actor.card()
