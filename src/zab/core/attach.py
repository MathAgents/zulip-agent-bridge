"""Bind native sessions without starting a turn (SPEC §4.6, §8.7, §9.8)."""

from __future__ import annotations

import json
import posixpath
import time
from dataclasses import asdict, replace
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from zab.config import merge_settings
from zab.core import presentation as view
from zab.core import types as t
from zab.core.creation import context_prompt, resolve_project
from zab.core.session import Reopen
from zab.core.watchdog import waiting
from zab.render.redact import redact_secrets
from zab.workspace.git import git
from zab.zulip.poster import ACK

if TYPE_CHECKING:
    from zab.core.bridge import BotBridge


async def attach(bridge: BotBridge, message: dict[str, Any], text: str, channel: dict) -> None:
    project = await resolve_project(bridge, message, channel)
    workspace = bridge.backend.workspace(project.path, project.path)
    # Attach uses the existing project, never allocates a worktree or creates a native session.
    prepared = await bridge.manager.prepare(workspace, "shared", message["subject"], project.subdir)
    workspace = bridge.backend.workspace(prepared.repo_root, prepared.cwd)
    native_id = text.removeprefix("!attach").strip()
    config = bridge.config.backends[bridge.bot.backend]
    if not native_id:
        sessions = await bridge.backend.list_sessions(prepared.cwd, limit=10)
        lines = ["Recent sessions (use !attach ID):"]
        for info in sessions[:10]:
            updated = (
                time.strftime("%Y-%m-%d %H:%M", time.localtime(info.updated_at))
                if info.updated_at
                else "unknown time"
            )
            # redacted before the cut, which can leave too little of a secret (§11.6, #661)
            title, preview = (
                redact_secrets((raw or "").replace("\x00", ""))[:80]
                for raw in (info.title, info.preview)
            )
            title = f"{view.code(title)} · " if title else ""
            lines.append(f"- {view.code(info.native_id)} · {title}{view.code(preview)} · {updated}")
            bridge.store.set(
                f"attach_settings:{bridge.bot.backend}:{info.native_id}", asdict(info.settings)
            )
        await bridge.poster.send(
            message["stream_id"],
            message["subject"],
            "\n".join(lines) if sessions else "No recent sessions for this project.",
        )
        return
    if any(c.isspace() for c in native_id):
        raise ValueError("Usage: !attach ID")
    previous = bridge.store.rows("sessions", backend=bridge.bot.backend, native_id=native_id)
    if any(row["state"] != "detached" for row in previous):
        raise ValueError("Session is already attached; use !detach in its topic first.")
    settings = merge_settings(
        {"approval": "never", "sandbox": "workspace-write"}
        if config.kind == "codex"
        else {"approval": "dontAsk"},
        config.defaults,
        project.backend_defaults.get(bridge.bot.backend, {}),
    )
    cached = bridge.store.get(f"attach_settings:{bridge.bot.backend}:{native_id}")
    if previous:
        settings = t.SessionSettings(
            **json.loads(max(previous, key=lambda r: r["updated_at"])["settings_json"])
        )
    elif cached:
        # Listings can omit settings (notably Codex); only populated fields override defaults.
        settings = replace(
            settings, **{k: v for k, v in cached.items() if v is not None and v != {}}
        )
    if config.kind == "codex" and previous:
        # Resume may replay approvals before the replacement binding exists.
        settings.extra["owned_turn_ids"] = [
            turn["native_turn_id"]
            for prior in previous
            for turn in bridge.store.rows("turns", session_id=prior["id"], state="running")
            if json.loads(turn["trigger_msg_ids"])
        ]
    found = await bridge.backend.lookup_session(native_id)
    if found is None:
        raise ValueError("No such native session.")
    if found.settings.account:
        settings.account = found.settings.account
    bridge.attaching[found.native_id] = []
    info = await bridge.backend.open_session(
        found.native_id,
        t.SessionSpec(
            git_identity=bridge.git_identity,
            bot=bridge.bot.name,
            cwd=found.cwd or prepared.cwd,
            name=message["subject"].removeprefix("✔ "),
            settings=settings,
            context_prompt=context_prompt(bridge.config.bridge.multi_party),
        ),
    )
    if await workspace.stat(info.cwd) is None:
        raise ValueError(
            "The session's working directory no longer exists; restore it before attaching."
        )
    if await workspace.realpath(info.cwd) != await workspace.realpath(prepared.cwd):
        # Explicit IDs retain their native cwd, including a previously created worktree.
        workspace = bridge.backend.workspace(info.cwd, info.cwd)
        root = await git(workspace, "rev-parse", "--show-toplevel")
        if root.exit_code:
            raise ValueError("Native workspace must be an existing git repository.")
        workspace = bridge.backend.workspace(root.stdout.strip(), info.cwd)
        prepared = await bridge.manager.prepare(
            workspace, "shared", message["subject"], posixpath.relpath(info.cwd, workspace.root)
        )
        workspace = bridge.backend.workspace(prepared.repo_root, prepared.cwd)
    if any(
        row["state"] != "detached"
        for row in bridge.store.rows(
            "sessions", backend=bridge.bot.backend, native_id=info.native_id
        )
    ):
        raise ValueError("Session is already attached; use !detach in its topic first.")
    snapshot = await bridge.manager.snapshot(workspace)
    row = dict(
        id=str(uuid4()),
        bot=bridge.bot.name,
        backend=bridge.bot.backend,
        native_id=info.native_id,
        stream_id=message["stream_id"],
        topic=message["subject"],
        workspace=prepared.cwd,
        workspace_mode="worktree"
        if any(
            r["workspace_mode"] == "worktree" and r["repo_root"] == prepared.repo_root
            for r in previous
        )
        else "shared",
        repo_root=prepared.repo_root,
        branch=prepared.branch,
        start_snapshot=json.dumps(asdict(snapshot)) if snapshot else None,
        settings_json=json.dumps(asdict(info.settings)),
        state="running" if info.active_turn_id else "idle",
        shared=0,
        last_sender_id=message["sender_id"],
        created_at=time.time(),
        updated_at=time.time(),
    )
    row["card_message_id"] = await bridge.poster.send(
        row["stream_id"],
        row["topic"],
        view.card(row, bridge.config.bridge.machine, info.resume_hint, 0),
        ACK,
    )
    bridge.store.insert("sessions", **row)
    bridge.store.update(
        "inbound",
        {"bot": bridge.bot.name, "message_id": message["id"]},
        session_id=row["id"],
        disposition="attach",
    )
    pending = {}
    for peer in bridge.peers:
        if old := peer.actors.pop(info.native_id, None):
            await waiting(
                old.inbox.join()
            )  # Drain resolutions queued before event buffering began.
            pending.update(old.pending)
            await old.close()
    if info.active_turn_id:
        for prior in sorted(previous, key=lambda r: r["updated_at"], reverse=True):
            turns = bridge.store.rows(
                "turns", session_id=prior["id"], native_turn_id=info.active_turn_id
            )
            if turns:
                # Keep ownership, process identity and diff baseline across binding changes.
                saved = turns[0] | dict(
                    session_id=row["id"],
                    state="running",
                    status_message_id=None,
                    final_message_ids=None,
                    ended_at=None,
                )
                saved.pop("id")
                with bridge.store.transaction():
                    bridge.store.insert("turns", **saved)
                    for source in previous:
                        bridge.store.update(
                            "turns",
                            {
                                "session_id": source["id"],
                                "native_turn_id": info.active_turn_id,
                                "state": "running",
                            },
                            state="superseded",
                            ended_at=time.time(),
                        )
                bridge.store.set(
                    f"client_ids:{row['id']}", bridge.store.get(f"client_ids:{prior['id']}", [])
                )
                break
    actor = bridge.make_actor(row, info.resume_hint)
    actor.ready = False
    # Reconcile active turns through the actor so events remain serialized.
    actor.put(info)
    await waiting(actor.inbox.join())
    for request in pending.values():
        if actor.turn and request.turn_id == actor.turn["native_turn_id"]:
            bridge.store.update(
                "approvals", {"request_id": request.request_id}, session_id=row["id"]
            )
            actor.put(request)  # Transfer before a refresh can fail, retaining controls for retry.
    for event in bridge.attaching.pop(found.native_id, []):
        actor.put(event)
    await waiting(actor.inbox.join())
    # Recheck once events have a bound actor: a turn may finish while its card is posted. The
    # actor takes this snapshot itself and applies it in the same step, as every reopen does
    # (§7.8.1); it leaves the session in error or backend-down if that fails.
    refresh = Reopen()
    actor.put(refresh)
    await waiting(actor.inbox.join())
    if refresh.info is None:
        await actor.post("Attached; refresh failed. Use !status to retry.")
        return
    info = refresh.info
    for rid, request in list(actor.pending.items()):
        if not actor.turn or request.turn_id != actor.turn["native_turn_id"]:
            await actor.resolve(rid, "resolved_elsewhere")
    if actor.pending:
        actor.state(state="awaiting_approval")
        await actor.card()
    recap = "**Attached session**\nLast user message:\n" + (info.preview or "(unavailable)")
    recap += "\n\nLast answer:\n" + (
        (info.last_turn.final_text if info.last_turn else None) or "(unavailable)"
    )
    for part in await bridge.renderer.render_agent_text(recap, actor.context):
        await actor.post(part)
