"""`!btw <question>` (SPEC §7.13, issue #716): a side question about the topic's conversation,
answered by its agent while a running turn goes on, and kept out of every history.

It runs beside the session's actor, never through it: no turn slot (§7.10), no turn row, no
status message, no snapshot. The backend answers from a throwaway copy of the conversation
(`Backend.side_question`). The answer goes into the topic marked as a side answer, every part
of it, so that peer context leaves it out (`peer_context.CONTROL`); the `!btw` message is a
command, which peer context leaves out too.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from zab.core import types as t
from zab.core.fork import parent_of

if TYPE_CHECKING:
    from zab.core.bridge import BotBridge
    from zab.core.session import SessionActor

USAGE = "Usage: !btw question"
#: How long the backend may take for one answer; then it stops what it started.
TIMEOUT_S = 120.0
#: An answer that took this long mentions its asker (§4.8), whatever `mention_after_s` says: a
#: side question never runs past `TIMEOUT_S`, which is that setting's default (review of #737).
SLOW_S = 60.0
#: How long a `!btw` waits for its session to reopen (the message that woke the VM).
READY_WAIT_S = 120.0
NOT_READY = "This session needs recovery first; use !status, then send !btw again."
NOT_SUPPORTED = "This agent can't answer side questions."
NO_ANSWER = "The agent gave no answer to the side question; try again."


def timed_out() -> str:
    return (
        f"The side question took more than {TIMEOUT_S:.0f} s, so the bridge stopped it; "
        "try again, or ask in the conversation."
    )


#: A part of a rendered answer whose first line is a block (a fence, a list, a heading, a
#: quote, a table, display math, indented code): it goes below the marker, not after it.
_BLOCK = re.compile(r"(?:```|~~~|#{1,6}\s|[-*+]\s|>|\||\d+[.)]\s|\$\$|\s{4})")


def is_btw(text: str) -> bool:
    return re.match(r"!btw(?:\s|$)", text) is not None


def marker(url: str, mention: str = "") -> str:
    """`💬 [btw](<the question>):`, which every part of an answer starts with; peer context
    leaves such messages out (`peer_context.CONTROL`)."""
    return f"💬 [btw]({url})" + (f" {mention}" if mention else "") + ":"


def start(bridge: BotBridge, message: dict[str, Any], text: str, user: dict[str, Any]) -> None:
    """Answer `!btw` in the background: the bot's other messages, `!stop` among them, are
    handled meanwhile. Bridge shutdown cancels it, and the backend stops what it started."""
    task = asyncio.create_task(answer(bridge, message, text, user["full_name"]))
    bridge.background.add(task)
    task.add_done_callback(bridge.background.discard)


async def answer(bridge: BotBridge, message: dict[str, Any], text: str, sender: str) -> None:
    """The side question's answer, or one ⚠️ line: a refusal (no question, no capability, a
    session in error or not reopened in time, no completed turn), a timeout or a failure."""
    log = logging.getLogger("zab.btw")
    try:
        question = text.removeprefix("!btw").strip()
        if not question:
            raise ValueError(USAGE)
        if not bridge.backend.capabilities.side_question:
            raise ValueError(NOT_SUPPORTED)
        row, actor = await _session(bridge, message)
        # taken in for the agent, now: idle stop counts it (`commands.status`), so the VM
        # doesn't power off in the middle of a side answer (review of #737)
        bridge.store.update(
            "inbound",
            {"bot": bridge.bot.name, "message_id": message["id"]},
            disposition="side_question",
            session_id=row["id"],
            handled_at=time.time(),
        )
        async with _one_at_a_time(bridge, row["id"], actor, message["id"]):
            await _react(actor, message["id"], "eyes")
            began = time.monotonic()
            reply = await bridge.backend.side_question(
                row["native_id"], question, timeout_s=TIMEOUT_S
            )
            if not reply.strip():
                raise ValueError(NO_ANSWER)
            await post(actor, message, sender, reply, time.monotonic() - began)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning("Side question: %s", type(exc).__name__, extra={"bot": bridge.bot.name})
        if isinstance(exc, TimeoutError):
            detail = timed_out()
        elif isinstance(exc, ValueError | t.SessionRefused | t.BackendUnavailable):
            detail = str(exc)
        else:
            detail = f"The side question failed ({type(exc).__name__}); try again."
        try:
            await bridge.poster.send(
                message["stream_id"],
                message["subject"],
                bridge.note(bridge.renderer.render_inline(detail)[:800]),
            )
        except Exception as error:  # its topic may be gone: nothing else to tell
            log.warning("Side question warning: %s", type(error).__name__)


async def _session(bridge: BotBridge, message: dict[str, Any]) -> tuple[dict, SessionActor]:
    """The topic's session and its actor, once it is open: a `!btw` replayed before the
    backend's reopen (the message that woke the VM) waits for it, up to `READY_WAIT_S`."""
    deadline = time.monotonic() + READY_WAIT_S
    while True:
        row, actor = parent_of(bridge, message)
        if row is None or actor is None or row["state"] == "error":
            raise ValueError(NOT_READY)
        if actor.ready:
            return row, actor
        if time.monotonic() >= deadline:
            raise ValueError(NOT_READY)
        await asyncio.sleep(1)


@contextlib.asynccontextmanager
async def _one_at_a_time(
    bridge: BotBridge, session_id: str, actor: SessionActor, message_id: int
) -> AsyncIterator[None]:
    """One side question per session at a time: a second waits for the first, with ⏳."""
    entry = bridge.side_questions.setdefault(session_id, [asyncio.Lock(), 0])
    entry[1] += 1
    try:
        if entry[0].locked():
            await _react(actor, message_id, "hourglass")
        async with entry[0]:
            yield
    finally:
        entry[1] -= 1
        if not entry[1]:
            bridge.side_questions.pop(session_id, None)


async def _react(actor: SessionActor, message_id: int, name: str) -> None:
    try:
        await actor.react(message_id, name)
    except Exception as exc:  # cosmetic: the answer comes either way
        actor.log_error("Side question reaction", exc)


async def post(
    actor: SessionActor, message: dict[str, Any], sender: str, reply: str, took: float
) -> None:
    """The answer, rendered as a final answer is (§11: links, uploads, math, splitting), with
    no turn behind it (no snapshot, no turn files), each part after the marker. A slow one
    mentions its asker in the first part's marker; a fast one never does (§4.8)."""
    site = actor.poster.client.site
    url = f"{site}/#narrow/channel/{message['stream_id']}/near/{message['id']}"
    mention = f"@**{sender}|{message['sender_id']}**" if took >= SLOW_S else ""
    heads = [marker(url, mention), marker(url)]
    context = replace(
        actor.context,
        final_answer=True,
        snapshot=None,
        turn_files=None,
        turn_number=None,
        max_message_length=actor.context.max_message_length - max(map(len, heads)) - 2,
    )
    parts = await actor.renderer.render_agent_text(reply, context)
    for i, part in enumerate(parts):
        separator = "\n\n" if _BLOCK.match(part) else " "
        await actor.post(heads[min(i, 1)] + separator + part)
