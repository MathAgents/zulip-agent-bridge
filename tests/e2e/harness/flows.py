"""Steps the scenarios share (issue #263): a DM command, a turn in a topic, the answer.

DMs to a bot take commands only (`!status`, `!vm`, `!help`, `!login`; `DM_HINT`), so a DM round
trip is a command and its reply. Conversations with the agents happen in topics of the test
participant's channel: a mention starts a session, and a follow-up without one continues it.
The agents are asked to echo tokens, so a reply is recognised by a word nobody else wrote.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any

from e2e.harness.zulip_user import TestUser, dm_narrow, mention, topic_narrow

#: How long a short agent answer may take: a new session, one model call, the posting.
ANSWER_S = 300.0
#: How long until a turn's status shows the command it runs (#754): it shows at the first look
#: that finds it, every 30 s from a minute into the turn, held to its edit gap of 60 s; after a
#: bridge restart, the agent's CLI may take minutes to start and to report its first action.
WORKING_S = 240.0
#: A turn's status message (`zab.core.presentation.status`), one per turn: by the shape of its
#: first line, `<heading> · <N>m <SS>s · <N> actions · <model>` (since #754, while it runs,
#: `<heading> · started <time:…> · …`), whatever the heading, or by a
#: known heading (the first post "⏳ **Working**", "· via terminal", "⏹ stopping…"). On #295 an
#: unlisted heading made a status message whose action list quoted the token pass for the answer.
#: (A topic's `!status` reply, "⏳ Running · … · N actions · N messages held", is not one.)
STATUS_RE = re.compile(
    r"^([^\n]*? · (?:\d+m \d{2}s|started <time:[^>\n]*>) · \d+ actions · (?:model default|`)"
    r"|⏳ \*\*Working\*\*|✅ \*\*Done\*\*|⏹ \*\*Stopped\*\*|❌ \*\*Failed\*\*"
    r"|⏸ \*\*Waiting for approval\*\*|⏹ stopping…)"
)
#: The bridge's reply to `!status` in a DM (`zab.core.bridge`).
DM_STATUS_RE = r"Bridge running"


def echo(token: str) -> str:
    return f"Reply with exactly `{token}` and nothing else. Don't run any tools."


def answer(pattern: str) -> str:
    """`pattern` in an answer of the agent, never in a turn's status message (whose commentary
    may quote the prompt)."""
    return r"^(?!" + STATUS_RE.pattern.removeprefix("^") + r")[\s\S]*?" + pattern


@dataclass
class Turn:
    """A message that started or continued an agent turn, and where its answer will be."""

    narrow: list[dict[str, Any]]
    message_id: int
    bot: int
    sent_at: float


class Chat:
    """The participant `zab-tester` in the sandbox channel and in DMs with the two bots."""

    def __init__(self, user: TestUser, bots: dict[str, int], names: dict[str, str], stream: int):
        self.user, self.bots, self.names, self.stream = user, bots, names, stream

    async def dm(self, kind: str, text: str) -> Turn:
        bot = self.bots[kind]
        mid = await self.user.send_dm(bot, text)
        return Turn(dm_narrow(bot), mid, bot, time.monotonic())

    async def say(self, kind: str, topic: str, text: str, *, mention_bot: bool = True) -> Turn:
        bot = self.bots[kind]
        content = f"{mention(self.names[kind], bot)} {text}" if mention_bot else text
        mid = await self.user.send(self.stream, topic, content)
        return Turn(topic_narrow(self.stream, topic), mid, bot, time.monotonic())

    async def reply(
        self, turn: Turn, pattern: str, *, within: float = ANSWER_S
    ) -> tuple[dict[str, Any], float]:
        """The bot's message matching `pattern` after the turn's message, and the seconds it
        took (measured here, so within one poll interval)."""
        msg = await self.user.wait_reply(
            turn.narrow, after_id=turn.message_id, senders=[turn.bot], pattern=pattern,
            within=within,
        )  # fmt: skip
        return msg, time.monotonic() - turn.sent_at

    async def bot_messages(self, turn: Turn) -> list[dict[str, Any]]:
        return [
            m
            for m in await self.user.after(turn.narrow, turn.message_id)
            if m["sender_id"] == turn.bot
        ]

    async def statuses(self, turn: Turn) -> list[dict[str, Any]]:
        """The turn status messages the bot posted after the turn's message."""
        return [m for m in await self.bot_messages(turn) if STATUS_RE.match(m["content"])]


def topic_name(scenario: int, nonce: str, kind: str = "") -> str:
    return f"e2e-{scenario}{'-' + kind if kind else ''}-{nonce}"
