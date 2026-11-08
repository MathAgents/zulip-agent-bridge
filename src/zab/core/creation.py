"""Resolve projects and prepare new session bindings (SPEC §5.4, §7.2)."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import posixpath
import re
import shlex
import time
import weakref
from dataclasses import asdict, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from zab.config import ProjectConfig, merge_settings
from zab.core import presentation as view
from zab.core import types as t
from zab.workspace.git import git
from zab.workspace.manager import subdir_cwd
from zab.zulip.poster import ACK

if TYPE_CHECKING:
    from zab.core.bridge import BotBridge
    from zab.core.session import SessionActor

CONTEXT = """This session is mirrored to a Zulip topic by zab. Your final message is posted as the
answer; make it self-contained. When you mention files in your answer, give their paths relative to
the repository root (for example fermat_little/README.md:1), not relative to a subdirectory.
Use $$...$$ for inline math and fenced math blocks for display math. Save images/PDFs inside
the repository and mention their paths. Attached files are under .zab/inbox/. Never print secrets.
Follow the repository's instructions about committing and pushing. Other agents may share this
topic; a message that mentions you is addressed to you, even when it also mentions another agent."""


def context_prompt(multi_party: bool) -> str:
    """Use the same owner boundary for creation, recovery, reopen and attach."""
    return CONTEXT + (
        "\nOther participants and their agents may be in this channel; act only for your "
        "user, and do not answer questions addressed to others unless your user asks."
        if multi_party
        else ""
    )


class _Options(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueError(message)


def new_options(text: str) -> tuple[argparse.Namespace, str]:
    """Parse only leading options; the prompt is opaque user text (SPEC §4.7)."""
    parser = _Options(add_help=False, exit_on_error=False)
    for flag in ("model", "effort", "approval", "account", "subdir"):
        parser.add_argument("--" + flag)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--worktree", action="store_true")
    mode.add_argument("--shared", action="store_true")
    argv: list[str] = []
    if text == "!new" or text.startswith("!new ") or text.startswith("!new\n"):
        text = text[4:].lstrip()
        while text.startswith("-"):
            lexer = shlex.shlex(text, posix=True)
            lexer.whitespace_split = True
            lexer.commenters = ""
            flag = lexer.get_token()
            if flag == "--":
                text = text[lexer.instream.tell() :].lstrip()
                break
            argv.append(flag)
            if flag in ("--model", "--effort", "--approval", "--account", "--subdir"):
                value = lexer.get_token()
                if value is None:
                    raise ValueError(f"{flag} requires a value")
                argv.append(value)
            text = text[lexer.instream.tell() :].lstrip()
    return parser.parse_args(argv), text


async def clone_project(bridge: BotBridge, channel: dict[str, Any]) -> ProjectConfig | None:
    if not getattr(bridge.config.bridge, "auto_clone", False):
        return None
    match = re.search(
        r"^repo:\s*([A-Za-z0-9][A-Za-z0-9_-]*)/([A-Za-z0-9][A-Za-z0-9_.-]*)\s*$",
        channel.get("description", ""),
        re.MULTILINE,
    )
    if not match:
        return None
    owner, name = match.groups()
    root = Path(bridge.config.bridge.projects_root)
    await asyncio.to_thread(root.mkdir, parents=True, exist_ok=True)
    path = root / name
    if not await asyncio.to_thread(path.exists):
        ws = bridge.backend.workspace(str(root), str(root))
        result = await git(
            ws,
            "clone",
            "--",
            f"https://github.com/{owner}/{name}.git",
            str(path),
            timeout_s=10,
        )
        if result.exit_code:
            raise ValueError("could not clone the channel repository with existing git credentials")
    else:
        ws = bridge.backend.workspace(str(path), str(path))
        top = await git(ws, "rev-parse", "--show-toplevel")
        origin = await git(ws, "remote", "get-url", "origin")
        remote = re.fullmatch(
            r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)([^/]+/[^/]+?)(?:\.git)?/?",
            origin.stdout.strip(),
        )
        if (
            top.exit_code
            or origin.exit_code
            or await asyncio.to_thread(Path(top.stdout.strip()).resolve)
            != await asyncio.to_thread(path.resolve)
            or not remote
            or remote[1].lower() != f"{owner}/{name}".lower()
        ):
            raise ValueError(
                "existing auto-clone destination does not match the channel repository"
            )
    return ProjectConfig(path=str(path))


async def resolve_project(
    bridge: BotBridge, message: dict[str, Any], channel: dict[str, Any]
) -> ProjectConfig:
    """Resolve the same configured project for new and attached sessions (§5.4)."""
    project = next(
        (p for p in bridge.config.projects if p.channel in (message["stream_id"], channel["name"])),
        None,
    )
    if project is None and bridge.config.bridge.default_project:
        project = ProjectConfig(path=bridge.config.bridge.default_project)
    if project is None:
        project = await clone_project(bridge, channel)
    if project is None:
        raise ValueError("add this channel to [[projects]] in your zab config")
    return project


#: One lock per topic, shared by every bot of this process (one bridge runs both bots on a
#: VM): the second agent's lookup of the first one's worktree and the first one's session
#: insertion must not interleave, or both prepare a worktree (codex on #168). Each bridge's
#: own `creation_lock` cannot order two bots.
_TOPIC_LOCKS: weakref.WeakValueDictionary[tuple[int, str], asyncio.Lock] = (
    weakref.WeakValueDictionary()
)


def topic_lock(stream_id: int, topic: str) -> asyncio.Lock:
    key = (int(stream_id), topic)
    lock = _TOPIC_LOCKS.get(key)
    if lock is None:
        lock = _TOPIC_LOCKS[key] = asyncio.Lock()
    return lock


def fork_topic_here(bridge: BotBridge, message: dict[str, Any]) -> bool:
    """Whether a live fork session (#202, SPEC §7.11), of any bot, is in this topic."""
    rows = bridge.store.rows("sessions", stream_id=message["stream_id"], topic=message["subject"])
    return any(
        row["parent_session_id"]
        and row["state"] != "detached"
        and row["workspace_mode"] == "worktree"
        for row in rows
    )


async def topic_worktree(
    bridge: BotBridge,
    message: dict[str, Any],
    project_root: str,
    subdir: str = "",
    *,
    session_id: str | None = None,
) -> t.PreparedWorkspace | None:
    """The worktree another bot's live session in this topic works in, if any, for reuse;
    with `session_id`, that session's only (a fork joining another bot's fork, #712).

    Only a non-detached session in worktree mode counts, and only while its checkout still
    exists; `base` stays None, so `discard` (which removes only what `prepare` created) never
    removes it if this session's creation fails. The repository and branch are the peer's,
    the cwd this session's own `subdir` in it (codex on #168, SPEC §10.1)."""
    rows = bridge.store.rows("sessions", stream_id=message["stream_id"], topic=message["subject"])
    for row in rows:
        if (
            row["bot"] == bridge.bot.name
            or row["state"] == "detached"
            or row["workspace_mode"] != "worktree"
            or session_id not in (None, row["id"])
        ):
            continue
        try:
            workspace = bridge.backend.workspace(project_root, project_root)
            base = await workspace.realpath(posixpath.join(project_root, ".zab", "worktrees"))
            candidate = await workspace.realpath(row["repo_root"])
            if candidate == base or posixpath.commonpath([base, candidate]) != base:
                continue
            check = await git(
                bridge.backend.workspace(row["repo_root"], row["repo_root"]),
                "rev-parse",
                "--show-toplevel",
            )
        except (OSError, TimeoutError):  # its directory is gone (codex on #168): no reuse
            continue
        if check.exit_code == 0:
            cwd = await subdir_cwd(
                bridge.backend.workspace(row["repo_root"], row["repo_root"]),
                row["repo_root"],
                subdir,
            )
            return t.PreparedWorkspace(
                repo_root=row["repo_root"], cwd=cwd, branch=row["branch"], base=None
            )
    return None


async def create_session(
    bridge: BotBridge, message: dict[str, Any], text: str, channel: dict[str, Any]
) -> tuple[SessionActor, str]:
    args, text = new_options(text)
    project = await resolve_project(bridge, message, channel)
    workspace = bridge.backend.workspace(project.path, project.path)
    check = await git(workspace, "rev-parse", "--show-toplevel")
    if check.exit_code:
        raise ValueError("project path must be a git repository")
    from zab.core.watchdog import waiting_lock

    # In either mode: a fork being created here (#202) holds this lock until its row exists.
    async with waiting_lock(topic_lock(message["stream_id"], message["subject"])):
        selected = (
            "worktree"
            if args.worktree
            else "shared"
            if args.shared
            # a fork's topic works in the fork's worktree, whatever the default (codex round 1
            # on #572): the other bot must not start in the checkout the fork was made from
            else "worktree"
            if fork_topic_here(bridge, message)
            else (project.workspace or bridge.config.bridge.default_workspace)
        )
        return await _create(bridge, message, text, args, project, workspace, selected)


async def _create(
    bridge: BotBridge,
    message: dict[str, Any],
    text: str,
    args: argparse.Namespace,
    project: ProjectConfig,
    workspace: t.Workspace,
    selected: str,
) -> tuple[SessionActor, str]:
    # One worktree per topic, not per agent (PM on issue #36, Mon 20:15): Claude and Codex in
    # one topic work in the same copy, so "@Claude write bound.py", then "@Codex run it" works.
    subdir = args.subdir or project.subdir
    peer = (
        await topic_worktree(bridge, message, workspace.root, subdir)
        if selected == "worktree"
        else None
    )
    prepared = peer or await bridge.manager.prepare(workspace, selected, message["subject"], subdir)
    bound = False
    try:
        workspace = bridge.backend.workspace(prepared.repo_root, prepared.cwd)
        snapshot = await bridge.manager.snapshot(workspace)
        settings = merge_settings(
            {"approval": "never", "sandbox": "workspace-write"}
            if bridge.config.backends[bridge.bot.backend].kind == "codex"
            else {"approval": "dontAsk"},
            bridge.config.backends[bridge.bot.backend].defaults,
            project.backend_defaults.get(bridge.bot.backend, {}),
            {
                k: getattr(args, k)
                for k in ("model", "effort", "approval", "account")
                if getattr(args, k) is not None
            },
        )
        session_id = str(uuid4())  # before create_session: ZAB_SESSION in the first turn (#655)
        spec = t.SessionSpec(
            cwd=prepared.cwd,
            name=message["subject"].removeprefix("✔ "),
            settings=settings,
            git_identity=bridge.git_identity,
            bot=bridge.bot.name,
            session=session_id,
            context_prompt=context_prompt(bridge.config.bridge.multi_party),
        )
        info = None
        for attempt in range(2):
            try:
                if info is None:
                    info = await bridge.backend.create_session(spec)
                if settings.model is not None or settings.effort is not None or attempt:
                    accepted = await bridge.backend.update_settings(info.native_id, settings)
                    info = replace(info, settings=accepted)
                break
            except t.InvalidSetting as exc:
                if (
                    attempt
                    or exc.field != "effort"
                    or args.effort is not None
                    or settings.effort is None
                ):
                    raise ValueError(view.invalid_setting(exc)) from None
                logging.getLogger("zab.creation").warning(
                    "Configured default effort unsupported; using the model default"
                )
                settings = replace(settings, effort=None)
                spec = replace(spec, settings=settings)
        assert info is not None
        row = dict(
            id=session_id,
            bot=bridge.bot.name,
            backend=bridge.bot.backend,
            native_id=info.native_id,
            stream_id=message["stream_id"],
            topic=message["subject"],
            workspace=prepared.cwd,
            workspace_mode=selected,
            repo_root=prepared.repo_root,
            branch=prepared.branch,
            start_snapshot=json.dumps(asdict(snapshot)) if snapshot else None,
            settings_json=json.dumps(asdict(info.settings)),
            state="idle",
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
        with bridge.store.transaction():
            bridge.store.insert("sessions", **row)
            key = f"pending_delivery:{bridge.bot.name}:{message['id']}"
            pending = bridge.store.get(key)
            if pending:
                bridge.store.set(key, pending | {"session_id": row["id"], "text": text})
            bridge.store.update(
                "inbound",
                {"bot": bridge.bot.name, "message_id": message["id"]},
                session_id=row["id"],
                disposition="turn",
            )
        bound = True
        return bridge.make_actor(row, info.resume_hint), text
    except BaseException:
        if not bound and peer is None:  # a reused worktree is the peer's: never removed here
            try:
                await bridge.manager.discard(prepared)
            except Exception as exc:
                logging.getLogger("zab.creation").warning(
                    "Workspace cleanup failed: %s", type(exc).__name__, exc_info=False
                )
        raise
