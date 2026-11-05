"""Settle detached turn records without posting or starting work (SPEC §7.8.1)."""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING

from zab.core import types as t
from zab.core.processes import is_bridge_child, reap_claude_turn
from zab.core.recovery import session_spec

if TYPE_CHECKING:
    from zab.core.bridge import BotBridge


def completed(bridge: BotBridge, event: t.TurnCompleted) -> None:
    """Detached bindings without an actor still observe a live completion."""
    actor = bridge.actors.get(event.session_id)
    for row in bridge.store.rows(
        "sessions",
        bot=bridge.bot.name,
        backend=bridge.bot.backend,
        native_id=event.session_id,
        state="detached",
    ):
        if actor and actor.row["id"] == row["id"]:
            continue  # actor completion also releases its slot and clears pending state
        bridge.store.update(
            "turns",
            {"session_id": row["id"], "native_turn_id": event.turn_id, "state": "running"},
            state=event.status.value,
            ended_at=time.time(),
        )


async def recover(bridge: BotBridge) -> None:
    groups: dict[str, list[dict]] = {}
    for row in bridge.store.rows("sessions", bot=bridge.bot.name, state="detached"):
        if bridge.store.rows("turns", session_id=row["id"], state="running"):
            groups.setdefault(row["native_id"], []).append(row)
    for native_id, rows in groups.items():
        successors = bridge.store.rows("sessions", backend=bridge.bot.backend, native_id=native_id)
        for row in rows:
            for turn in bridge.store.rows("turns", session_id=row["id"], state="running"):
                if any(
                    successor["state"] != "detached"
                    and bridge.store.rows(
                        "turns", session_id=successor["id"], native_turn_id=turn["native_turn_id"]
                    )
                    for successor in successors
                ):
                    # The replacement owns this turn; no backend query is needed to retire
                    # the old binding, even if the new bot has a different git identity.
                    bridge.store.update(
                        "turns",
                        {"id": turn["id"], "state": "running"},
                        state="superseded",
                        ended_at=time.time(),
                    )
        rows = [r for r in rows if bridge.store.rows("turns", session_id=r["id"], state="running")]
        if not rows:
            continue
        actor = bridge.actors.get(native_id)
        owner = next(
            (
                peer.actors[native_id]
                for peer in bridge.peers
                if peer.bot.backend == bridge.bot.backend and native_id in peer.actors
            ),
            actor,
        )
        latest = max(rows, key=lambda row: row["updated_at"])
        spec = (
            session_spec(owner)
            if owner
            else t.SessionSpec(
                cwd=latest["workspace"],
                name=latest["topic"],
                settings=t.SessionSettings(**json.loads(latest["settings_json"])),
                git_identity=getattr(bridge, "git_identity", None),
                bot=bridge.bot.name,
            )
        )
        try:
            info = await bridge.backend.open_session(native_id, spec)
            if actor is not bridge.actors.get(native_id):
                actor = None  # an attach may have drained and closed it during the query
            if actor and actor.row["state"] == "detached":
                actor.put(info)
                await actor.inbox.join()
            for row in rows:
                if actor and actor.row["id"] == row["id"]:
                    continue
                for turn in bridge.store.rows("turns", session_id=row["id"], state="running"):
                    tid = turn["native_turn_id"]
                    if tid == info.active_turn_id:
                        continue
                    if bridge.config.backends[bridge.bot.backend].kind == "claude" and turn["pgid"]:
                        if is_bridge_child(turn["pgid"]):
                            continue
                        launch, separator, _ = tid.partition(":")
                        await reap_claude_turn(
                            turn["pgid"],
                            launch if separator else native_id,
                            bridge.config.backends[bridge.bot.backend].binary,
                        )
                    status = (
                        info.last_turn.status
                        if info.last_turn and info.last_turn.turn_id == tid
                        else t.TurnStatus.INTERRUPTED
                    )
                    bridge.store.update(
                        "turns",
                        {"id": turn["id"], "state": "running"},
                        state=status.value,
                        ended_at=time.time(),
                    )
        except Exception as exc:
            # Unknown backend state must continue blocking idle stop; try on next reconnect.
            logging.getLogger("zab.recovery").warning(
                "Detached turn recovery failed (%s)",
                type(exc).__name__,
                extra={"bot": bridge.bot.name, "session": latest["id"]},
            )
