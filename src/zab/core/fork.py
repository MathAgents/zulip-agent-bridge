"""`!fork [--name NAME] prompt` (SPEC §7.11, issue #202): the prompt runs in parallel, in a new
topic, in a copy of this topic's session through its last completed turn, on a worktree branched
from this topic's files as they stand. The parent's session, transcript or thread, worktree and
running turn are only read."""

from __future__ import annotations

import contextlib
import json
import logging
import posixpath
import re
import shlex
import time
from collections.abc import AsyncIterator
from dataclasses import asdict, replace
from typing import TYPE_CHECKING, Any
from urllib.parse import quote
from uuid import uuid4

from zab.core import presentation as view
from zab.core import types as t
from zab.core.creation import context_prompt, topic_lock, topic_worktree
from zab.core.watchdog import waiting_lock
from zab.zulip.poster import ACK

if TYPE_CHECKING:
    from zab.core.bridge import BotBridge
    from zab.core.session import SessionActor
    from zab.core.store import Store

FORK = "⑂"
USAGE = "Usage: !fork [--name NAME] prompt"
#: How much of a fork's answer its parent's next input carries; the rest is behind its link.
ANSWER_MAX = 8000
NOT_READY = "This session needs recovery before a fork; use !status, then send !fork again."


def is_fork(text: str) -> bool:
    return re.match(r"!fork(?:\s|$)", text) is not None


def fork_options(text: str) -> tuple[str | None, str]:
    """`--name NAME` or `--name=NAME`, and `--`, before the prompt. Anything else starts the
    prompt, which is opaque user text (§4.7): a Markdown list (`- case 1`), `-1 is a root?`
    (review of #572)."""
    text = text.removeprefix("!fork").lstrip()
    name = None
    while option := re.match(r"--(?:name(?=[=\s]|$)|(?=\s|$))", text):
        if option[0] == "--":
            text = text[2:].lstrip()
            break
        lexer = shlex.shlex(text, posix=True)
        lexer.whitespace_split = True
        lexer.commenters = ""
        try:
            flag = lexer.get_token() or ""
            value = lexer.get_token() if flag == "--name" else flag.removeprefix("--name=")
        except ValueError:  # an unclosed quote
            raise ValueError(USAGE) from None
        text = text[lexer.instream.tell() :].lstrip()
        # one line of printable text: it becomes part of a topic name
        name = "".join(c for c in " ".join((value or "").split()) if c.isprintable())
        if not name:
            raise ValueError(USAGE)
    if not text:
        raise ValueError(USAGE)
    return name, text


def _waiting(bot: str) -> str:
    return f"pending_forks:{bot}"


def defer_fork(store: Store, bot: str, message: dict[str, Any], text: str, sender: str) -> None:
    """Keep a `!fork` whose session hasn't reopened yet (after a restart, or while its backend
    is down) for `BotBridge.run_pending_forks`, once (review of #572)."""
    with store.transaction():
        waiting = store.get(_waiting(bot), [])
        if any(w["message_id"] == message["id"] for w in waiting):
            return
        entry = dict(
            message_id=message["id"],
            stream_id=message["stream_id"],
            subject=message["subject"],
            sender_id=message["sender_id"],
            sender=sender,
            text=text,
        )
        store.set(_waiting(bot), [*waiting, entry])


def pending_forks(store: Store, bot: str) -> list[dict[str, Any]]:
    return sorted(store.get(_waiting(bot), []), key=lambda w: w["message_id"])


def drop_pending_fork(store: Store, bot: str, message_id: int) -> None:
    """Joins the caller's transaction: a deferred fork goes with the row it created."""
    with store.transaction():
        waiting = store.get(_waiting(bot), [])
        left = [w for w in waiting if w["message_id"] != message_id]
        if len(left) != len(waiting):
            store.set(_waiting(bot), left)


def fork_topic(parent: str, label: str, limit: int) -> str:
    """`<parent topic> ⑂ <label>` within Zulip's topic length, shortening the parent's part
    first (with "…"), and the label to half the length at most. A resolved topic's "✔ " goes."""
    base, suffix = parent.removeprefix("✔ ").strip(), f" {FORK} {label}"
    if len(suffix) > limit // 2:
        suffix = suffix[: limit // 2 - 1] + "…"
    if len(base) > limit - len(suffix):
        base = base[: limit - len(suffix) - 1].rstrip() + "…"
    return base + suffix


def topic_link(site: str, stream_id: int, channel: str, topic: str) -> str:
    """Zulip's `#**channel>topic**`, or the topic's URL when a name would break that syntax."""
    if channel and not any(c in channel for c in "*>`\n") and not any(c in topic for c in "*`\n"):
        return f"#**{channel}>{topic}**"
    encoded = quote(topic, safe="").replace(".", "%2E").replace("%", ".")  # Zulip's hash encoding
    return f"{site}/#narrow/channel/{stream_id}/topic/{encoded}"


def _project(row: dict[str, Any]) -> tuple[str, str]:
    """The checkout the parent's worktree belongs to, and the parent's subdirectory in it: a new
    worktree goes beside the parent's (`<project>/.zab/worktrees/`, §10.1), never inside it."""
    root = row["repo_root"].rstrip("/")
    parent, _ = posixpath.split(root)
    zab, worktrees = posixpath.split(parent)
    project, dot_zab = posixpath.split(zab)
    if row["workspace_mode"] == "worktree" and (dot_zab, worktrees) == (".zab", "worktrees"):
        root = project
    subdir = posixpath.relpath(row["workspace"], row["repo_root"])
    return root, "" if subdir == "." else subdir


def _choose_topic(bridge: BotBridge, stream_id: int, parent: str, name: str | None) -> str:
    """The fork topic, which no session of any bot has had (Zulip compares topics without case)."""
    limit = bridge.client.limits.get("max_topic_length", 60)
    taken = {row["topic"].casefold() for row in bridge.store.rows("sessions", stream_id=stream_id)}
    if name is not None:
        topic = fork_topic(parent, name, limit)
        if topic.casefold() in taken:
            raise ValueError(f"The topic “{topic}” already has a session; choose another --name.")
        return topic
    n = 1
    while (topic := fork_topic(parent, str(n), limit)).casefold() in taken:
        n += 1
    return topic


def parent_of(bridge: BotBridge, message: dict[str, Any]) -> tuple[dict | None, Any]:
    """This bot's live session in the message's topic, and its actor."""
    rows = bridge.store.rows(
        "sessions", bot=bridge.bot.name, stream_id=message["stream_id"], topic=message["subject"]
    )
    parent = next((r for r in rows if r["state"] != "detached"), None)
    return parent, bridge.actors.get(parent["native_id"]) if parent else None


def waits(bridge: BotBridge, message: dict[str, Any]) -> bool:
    """Whether the session to fork is reopening (after a restart, or while its backend is
    down): the fork then waits for it (`defer_fork`) instead of being refused."""
    parent, actor = parent_of(bridge, message)
    return (
        parent is not None and actor is not None and not actor.ready and parent["state"] != "error"
    )


def fork_of_message(message_id: int) -> str:
    """The store key of the fork the first of the VM's bots made of a `!fork` message: its
    session's id, which the others' forks of that message join (#712)."""
    return f"fork_of_message:{message_id}"


def _first_fork(bridge: BotBridge, message: dict[str, Any]) -> dict[str, Any] | None:
    """Another bot's fork of this very `!fork` message (both bots got it, #712), while that
    session is live in this channel."""
    session_id = bridge.store.get(fork_of_message(message["id"]))
    row = next(iter(bridge.store.rows("sessions", id=session_id)), None) if session_id else None
    if (
        row is None
        or row["bot"] == bridge.bot.name
        or row["stream_id"] != message["stream_id"]
        or row["state"] in ("detached", "readonly")
    ):
        return None
    return row


async def _join(
    bridge: BotBridge, message: dict[str, Any], parent: dict[str, Any], first: dict[str, Any]
) -> t.PreparedWorkspace | None:
    """The worktree of `first`'s topic for this bot's fork, under that topic's creation lock:
    None if `first` has left the topic meanwhile, if this bot has a session there, or if the
    first's worktree is gone (`topic_worktree`, which gives this bot's own subdirectory in it)."""
    now = _first_fork(bridge, message)
    if now is None or (now["id"], now["topic"]) != (first["id"], first["topic"]):
        return None
    mine = bridge.store.rows(
        "sessions", bot=bridge.bot.name, stream_id=first["stream_id"], topic=first["topic"]
    )
    if any(r["state"] != "detached" for r in mine):
        return None
    root, subdir = _project(parent)
    here = {"stream_id": first["stream_id"], "subject": first["topic"]}
    return await topic_worktree(bridge, here, root, subdir, session_id=first["id"])


@contextlib.asynccontextmanager
async def _destination(
    bridge: BotBridge, message: dict[str, Any], parent: dict[str, Any], name: str | None
) -> AsyncIterator[tuple[str, t.PreparedWorkspace | None]]:
    """The fork topic, under the creation lock of that topic (`topic_lock`), held until the
    fork's row exists: a session another bot starts there meanwhile waits for it, then works
    in the fork's worktree (codex round 1 on #572). One that took the topic first wins.

    With it, the worktree to join, if another bot of the VM has forked its session on this
    same message (#712): its topic, `--name`'s included, and its worktree. Else None: a topic
    of this fork's own, where a `--name` that another session holds is refused."""
    stream_id = message["stream_id"]
    if (first := _first_fork(bridge, message)) is not None:
        async with waiting_lock(topic_lock(stream_id, first["topic"])):
            if (joined := await _join(bridge, message, parent, first)) is not None:
                yield first["topic"], joined
                return
    topic = _choose_topic(bridge, stream_id, message["subject"], name)
    async with waiting_lock(topic_lock(stream_id, topic)):
        if any(
            r["topic"].casefold() == topic.casefold()
            for r in bridge.store.rows("sessions", stream_id=stream_id)
        ):
            raise ValueError(f"The topic “{topic}” was just taken; send !fork again.")
        yield topic, None


async def fork(
    bridge: BotBridge, message: dict[str, Any], text: str, sender: str, channel: dict[str, Any]
) -> None:
    """Create the fork of the session in `message`'s topic and hand it the prompt (§7.11).

    The fork's topic, worktree (from `fork_base`, the parent's files with their uncommitted
    changes), native session (`fork_session`), card with a back-link, then its row and its
    first prompt, in one transaction, as a pending delivery its actor runs through the turn
    slots like any other. A failure before the row exists removes the new worktree and raises:
    ValueError with a reason for the user. The pointer line in the parent is posted last.

    Where another bot of the VM forked its session on this message first (#712), this fork
    joins that topic and worktree instead: no `fork_base`, no worktree of its own to remove,
    and no second pointer line.
    """
    name, prompt = fork_options(text)
    if not bridge.backend.capabilities.fork:
        raise ValueError("This agent can't fork its sessions.")
    stream_id, parent_topic = message["stream_id"], message["subject"]
    parent, actor = parent_of(bridge, message)
    if parent is None or actor is None or not actor.ready or parent["state"] == "error":
        raise ValueError(NOT_READY)
    site = bridge.client.site
    # Forks in one channel, by either bot of this process, choose their topics one at a time:
    # the second fork of one `!fork` message finds the first's (#712). A fork is a creation
    # of this bot's, as `create_pending` is: a fork that waited runs beside the listener, and
    # a message in its topic before its row exists waits for it, then reaches it, instead of
    # starting a second session of this bot there (review of #742). Locks in this order:
    # the channel's fork lock, the bot's creation lock, the fork topic's.
    async with (
        waiting_lock(topic_lock(stream_id, "\0fork")),
        waiting_lock(bridge.creation_lock),
        _destination(bridge, message, parent, name) as (topic, joined),
    ):
        prepared = (
            joined if joined is not None else await _worktree(bridge, message, parent, actor, topic)
        )
        bound = False
        try:
            workspace = bridge.backend.workspace(prepared.repo_root, prepared.cwd)
            snapshot = await bridge.manager.snapshot(workspace)
            # The parent's model, effort, approval, sandbox and account; its native bookkeeping
            # (`extra`) is its own. The backend lays these over the parent's effective settings.
            settings = replace(t.SessionSettings(**json.loads(parent["settings_json"])), extra={})
            info = await bridge.backend.fork_session(
                parent["native_id"],
                t.SessionSpec(
                    cwd=prepared.cwd,
                    name=topic,
                    settings=settings,
                    git_identity=bridge.git_identity,
                    bot=bridge.bot.name,
                    context_prompt=context_prompt(bridge.config.bridge.multi_party),
                ),
            )
            now = time.time()
            row = dict(
                id=str(uuid4()),
                bot=bridge.bot.name,
                backend=bridge.bot.backend,
                native_id=info.native_id,
                stream_id=stream_id,
                topic=topic,
                workspace=prepared.cwd,
                workspace_mode="worktree",
                repo_root=prepared.repo_root,
                branch=prepared.branch,
                start_snapshot=json.dumps(asdict(snapshot)) if snapshot else None,
                settings_json=json.dumps(asdict(info.settings)),
                state="idle",
                shared=0,
                last_sender_id=message["sender_id"],
                parent_session_id=parent["id"],
                created_at=now,
                updated_at=now,
            )
            back = topic_link(site, stream_id, channel.get("name", ""), parent_topic)
            files = (
                " and its files, on a branch of its own"
                if joined is None
                else ", in the files and on the branch that this topic's other agent works on"
            )
            row["card_message_id"] = await bridge.poster.send(
                stream_id,
                topic,
                f"**zab session** · {FORK} Forked from {back}, with its conversation through its "
                f"last completed turn{files}. Write in this topic to continue (mention me if "
                "others are in the channel); !help lists commands, !session shows details.",
                ACK,
            )
            key = f"pending_delivery:{bridge.bot.name}:{message['id']}"
            with bridge.store.transaction():
                bridge.store.insert("sessions", **row)
                if joined is None:  # the fork the VM's other bots join (#712)
                    bridge.store.set(fork_of_message(message["id"]), row["id"])
                drop_pending_fork(bridge.store, bridge.bot.name, message["id"])  # if deferred
                bridge.store.set(
                    key,
                    dict(
                        message_id=message["id"],
                        sender_id=message["sender_id"],
                        sender=sender,
                        text=prompt,
                        session_id=row["id"],
                        message=None,
                    ),
                )
                bridge.store.update(
                    "inbound",
                    {"bot": bridge.bot.name, "message_id": message["id"]},
                    session_id=row["id"],
                    disposition="turn",
                )
            bound = True
        except BaseException:
            if not bound and joined is None:  # a joined worktree is the other fork's
                try:
                    await bridge.manager.discard(prepared)
                except Exception as exc:
                    logging.getLogger("zab.fork").warning(
                        "Fork worktree cleanup failed: %s", type(exc).__name__
                    )
            raise
    bridge.make_actor(row, info.resume_hint)  # takes the pending prompt: the fork's first turn
    if joined is not None:
        return  # the parent topic has the first fork's pointer
    link = topic_link(site, stream_id, channel.get("name", ""), topic)
    try:
        await bridge.poster.send(stream_id, parent_topic, f"{FORK} forked to {link}")
    except Exception as exc:  # the fork runs either way; its card links back here
        logging.getLogger("zab.fork").warning("Fork pointer not posted: %s", type(exc).__name__)


async def _worktree(
    bridge: BotBridge,
    message: dict[str, Any],
    parent: dict[str, Any],
    actor: SessionActor,
    topic: str,
) -> t.PreparedWorkspace:
    """Steps 1 and 2 of §7.11: the fork's own worktree, beside the parent's, on a commit of
    the parent's files as they stand."""
    base = await bridge.manager.fork_base(
        actor.context.workspace, f"{parent['id']}/{message['id']}"
    )
    if base is None:
        raise ValueError(
            "A fork needs this session's workspace to be a git repository with a commit."
        )
    root, subdir = _project(parent)
    project = bridge.backend.workspace(root, root)
    return await bridge.manager.prepare(project, "worktree", topic, subdir, base_commit=base)


async def report_fork(actor: SessionActor, event: t.TurnCompleted) -> None:
    """Step 4 of #202 (SPEC §7.11): a fork's first turn ended and its answer is posted. Once per
    fork, the answer is kept for its parent's next input (`peer_context.fork_context`), and the
    parent topic gets one line linking to it. Never raises: the fork's own completion stands."""
    row, store = actor.row, actor.store
    key = f"fork_reported:{row['id']}"
    if store.get(key) or actor.turn is None:
        return
    if store.get(f"restart_notice:{actor.turn['id']}"):
        # A restart ended it and no answer was posted: the turn the user's "continue" starts
        # is the one reported (review of #580).
        return
    try:
        ids = json.loads(actor.turn["final_message_ids"] or "[]")
        site = actor.poster.client.site
        url = f"{site}/#narrow/channel/{row['stream_id']}/near/{ids[0]}" if ids else None
        answer = event.final_text or ""
        truncated = len(answer) > ANSWER_MAX  # the stored part; the block may cut it further
        parent = next(iter(store.rows("sessions", id=row["parent_session_id"])), None)
        live = parent is not None and parent["state"] not in ("detached", "readonly")
        with store.transaction():
            store.set(key, True)
            if live:
                kept = store.get(f"fork_answers:{parent['id']}", [])
                found = dict(
                    session_id=row["id"],
                    fork=row["topic"],
                    branch=row["branch"],
                    status=event.status.value,
                    url=url,
                    answer=answer[:ANSWER_MAX],
                    truncated=truncated,
                )
                store.set(f"fork_answers:{parent['id']}", [*kept, found])
        if not live:
            return
        if actor.confirm_location:
            await actor.confirm_location(parent["id"])  # post only at a confirmed card (§4.4)
            parent = store.rows("sessions", id=parent["id"])[0]
            if parent["state"] in ("detached", "readonly"):
                return
        ended = "answered" if event.status == t.TurnStatus.COMPLETED else event.status.value
        await actor.poster.send(
            parent["stream_id"],
            parent["topic"],
            f"{FORK} {view.code(row['topic'])} {ended}" + (f": {url}" if url else ""),
        )
    except Exception as exc:
        logging.getLogger("zab.fork").warning(
            "Fork report failed: %s", type(exc).__name__, extra={"session": row["id"]}
        )
