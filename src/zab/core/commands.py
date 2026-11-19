"""Topic command text and workspace operations (SPEC §4.6, §10.5, §11.4)."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from dataclasses import replace
from typing import TYPE_CHECKING

from zab.core import types as t
from zab.core.creation import new_options
from zab.core.presentation import AGENT_NAMES
from zab.core.reason import asked_by
from zab.render.links import upload_file
from zab.render.mentions import disarm_mentions

if TYPE_CHECKING:
    from zab.core.session import Delivery, SessionActor

HELP = """In your own channel, `@claude` or `@codex` talks to one agent; naming neither, to both.
`!stop` — interrupt the running turn (queued messages are kept), or sub-agents left running.
`!status` — current state.
`!queue [clear]` — show or drop messages waiting for the current turn.
`!stop NAME`, `!stop jobs` — stop this topic's background job NAME, or all; `!stop` alone never.
`!new [opts] prompt` — start a session (in a new topic; `--model`, `--effort`, `--worktree`, …).
`!fork [--name NAME] prompt` — run the prompt in parallel, in a new topic, from a copy of this one.
`!btw question` — ask me about this conversation without adding to it or stopping my work.
`!model [M]`, `!effort [E]` — show or change the model or reasoning effort.
`!usage` — account limits, context, session tokens and the VM's CPU, memory and disk use.
`!vm` — how busy the VM's CPUs, memory and disk are, and the bridge's state (also in a DM).
`!tools` — the software on this VM: versions and how to call each tool (also in a DM).
`!diff` — changes since the session started.
`!file PATH` — upload an allowed file from the workspace.
`!handover` — have the agent update HANDOVER.md, commit and push.
`!session` — session details.
`!approve`, `!approve-session`, `!deny` — answer an approval request.
`!attach [ID]`, `!detach` — list, attach or detach a native session.
`!login` — connect your subscription (in a direct message to this bot).
`!unmentioned [all|last]` — naming neither agent: to both, or to the topic's last named (in a DM).
`!help` — this list."""
DM_HELP = (
    "In a direct message, only `!login`, `!status`, `!vm`, `!tools`, `!restart`, "
    "`!unmentioned` and `!help` work. If your bots stop answering, send `!vm` here: it "
    "answers even then, and says whether your bridge runs; `!restart` restarts the bridge, "
    "interrupting running turns. `!restart codex` restarts Codex, which also frees memory held "
    "by idle Lean servers. The other commands below are for channel topics.\n\n" + HELP
)
UNKNOWN = "Unknown command; use !help."
#: The command that detaches a session from its topic: exactly this (`SessionActor.command`),
#: as the topic's addressee record reads it too (`Router._record`, #891).
DETACH = "!detach"
#: `!stop` with no turn running (#847, SPEC §7.7)
NO_TURN = "No turn is running."
NO_TURN_NOR_SUBAGENT = "No turn is running, and no sub-agent was."
SUBAGENTS_UNCHECKED = (
    "No turn is running, but a sub-agent an earlier turn started may still run: not every "
    "one could be stopped or checked. Try !stop again."
)
TURN_ELSEWHERE = (
    "A turn started outside Zulip, such as in a terminal, is running on this session, so "
    "nothing was stopped: stop it where it runs."
)


def stopped_subagents(count: int, *, unseen: bool = False) -> str:
    """The answer to a `!stop` with no turn that stopped `count` sub-agents, or that can't
    rule one out (`unseen`: one the backend couldn't see, or check, may run)."""
    which = "1 sub-agent" if count == 1 else f"{count} sub-agents"
    text = (
        f"No turn is running; stopped {which} that an earlier turn left running."
        if count
        else "No turn is running, and no sub-agent the bridge saw start is running."
    )
    if unseen:  # a backend that can't see them all, or one whose look failed in part
        text = text.removesuffix(".") + ", but not every one could be checked."
    return text


FORK_NO_SESSION = (
    "No session here to fork: write a prompt to start one, then !fork it; !help lists the commands."
)
FORK_DM = "!fork works in a topic with a session: send it there, and it forks that session."
BTW_NO_SESSION = (
    "No session here to ask about: write a prompt to start one, then !btw; !help lists the "
    "commands."
)
BTW_DM = "!btw works in a topic with a session: send it there, and I answer about that topic."
NEW_DM = "!new works in a channel topic: send it in a new topic, and it starts a session there."


def is_new(text: str) -> bool:
    """Whether a command is `!new`, with or without options and a prompt."""
    return text.split(maxsplit=1)[:1] == ["!new"]


def _new_parts(command: str) -> tuple[bool, bool]:
    """Whether a `!new` command carries a prompt after its options, and whether those set a
    model or an effort. With options it can't read, each word after `!new` that isn't an
    option may be a prompt, so that one is never dropped without a word (#985)."""
    try:
        options, prompt = new_options(command)
    except (ValueError, argparse.ArgumentError):
        return any(not word.startswith("-") for word in command.split()[1:]), False
    return bool(prompt.strip()), bool(options.model or options.effort)


def _listed(items: list[str], last: str) -> str:
    """`A`, `A and B`, `A, B and C`, for `last` "and"; "or" likewise."""
    return f"{', '.join(items[:-1])} {last} {items[-1]}" if len(items) > 1 else items[0]


def new_with_session(kind: str, command: str) -> str:
    """The answer to a `!new` that reaches a bot with a session in the topic, named or as the
    one bot an unmentioned one reaches (SPEC §4.6, #985): it starts nothing, and a prompt after
    its options was not sent. `kind` is the bot's backend kind."""
    return new_with_sessions([kind], command)


def new_with_sessions(kinds: list[str], command: str) -> str:
    """`new_with_session` for the topic's sessions of the agents of `kinds`: one bot's, or, from
    the bot that answers for the topic, those of the bots an unmentioned `!new` reached where
    none started a session with it (SPEC §4.6, #987). After `!detach`, a session starts on a
    mention (§4.5), and options without a prompt likely meant `!model` or `!effort`, which
    change a running session's."""
    agents = [AGENT_NAMES.get(kind, kind) for kind in kinds]
    one = len(agents) == 1
    has = _listed([f"a {agent} session" for agent in agents], "and")
    new = (
        f"for {'a new one' if one else 'new ones'}, start a new topic, or use !detach first, "
        f"then mention {'me' if one else 'the agents you want'}."
    )
    prompt, settings = _new_parts(command)
    if prompt:
        return (
            f"This topic already has {has}, so your prompt wasn't sent to "
            f"{_listed(agents, 'or')}. To continue {'that session' if one else 'those sessions'}, "
            f"send the prompt again without !new; {new}"
        )
    whose = "this session's" if one else "those sessions'"
    hint = f" To change {whose} model or effort, use !model or !effort."
    return f"This topic already has {has}; {new}" + (hint if settings else "")


def new_in_dm(kind: str, command: str) -> str:
    """The answer to `!new` in a direct message, where no session starts (SPEC §4.6, #987): a
    session is a channel topic's, and a DM takes only commands. A prompt after its options was
    not sent to the bot's agent (`kind`)."""
    if not _new_parts(command)[0]:
        return NEW_DM
    return (
        f"Your prompt wasn't sent to {AGENT_NAMES.get(kind, kind)}: !new works in a channel "
        "topic, so send it in a new topic, and it starts a session there with your prompt."
    )


DM_HINT = (
    "DMs only take commands. Start a topic in your sandbox channel to talk to your "
    "agents; !help lists commands."
)
HANDOVER = """Update HANDOVER.md at the repository root so that someone new (a person or a fresh agent session) could
continue this work without this conversation. Include: the goal; the current state (what works, what does
not); the key decisions and why; the open problems and next steps; the exact commands to reproduce the main
results; and pointers to the relevant files. Keep it under about 150 lines. Then commit HANDOVER.md together
with any other finished work and push. End your reply with a short summary, the full commit hash
(`git rev-parse HEAD`) and the path `HANDOVER.md`."""  # noqa: E501


async def status_login_hint(backend: t.Backend) -> str:
    """Only explicit signed-out checks warrant a hint; failures mean unknown."""
    try:
        async with asyncio.timeout(2):  # Keep a slow doctor from holding up topic commands.
            with asked_by("!status"):
                checks = await backend.doctor()
        accounts = [
            c for c in checks if c.name == "codex:login" or c.name.startswith("claude account ")
        ]
        if accounts and all(
            not c.ok and (c.name == "codex:login" or c.detail.startswith("not signed in:"))
            for c in accounts
        ):
            return " · sign in via DM: !login"
    except Exception:
        return ""  # No diagnostic text or guessed auth state in a status reply.
    return ""


async def run_command(actor: SessionActor, command: str, delivery: Delivery | None) -> bool:
    """Keep read-only command failures from changing the agent session state."""
    if command not in {"!session", "!usage", "!diff", "!file"} and not command.startswith("!file "):
        return await _run_command(actor, command, delivery)
    try:
        return await _run_command(actor, command, delivery)
    except Exception as exc:
        actor.log_error("Workspace command", exc)
        warning = (
            "⚠️ Session details unavailable; try !session again."
            if command == "!session"
            else "⚠️ Usage unavailable; try !usage again."
            if command == "!usage"
            else "⚠️ No diff available; try !diff again."
            if command == "!diff"
            else "⚠️ File upload failed; try !file again."
        )
        try:
            await actor.post(warning)
        except Exception as post_error:
            actor.log_error("Workspace command warning", post_error)
        return True


async def _run_command(actor: SessionActor, command: str, delivery: Delivery | None) -> bool:
    if command == "!help":
        await actor.post(HELP)
    elif command == "!session":
        from zab.core.session_details import session_details

        where = actor.unmentioned_here  # where a message without a mention goes here (#878)
        line = await where(actor.row["stream_id"], actor.row["topic"]) if where else ""
        await actor.post(session_details(actor, line))
    elif command == "!usage":
        from zab.core.usage import usage_reply

        await actor.post(await usage_reply(actor))
    elif command == "!diff":
        if not actor.row.get("start_snapshot"):
            await actor.post("No session-start snapshot is available.")
            return True
        data = json.loads(actor.row["start_snapshot"])
        snapshot = t.Snapshot(ref=data["ref"], untracked=tuple(data.get("untracked", ())))
        diff = await actor.manager.diff(actor.context.workspace, snapshot)
        if diff is None:
            await actor.post("No diff available.")
        elif diff.empty:
            await actor.post("No changes since session start.")
        else:
            await actor.post(await actor.renderer.render_diff(diff, actor.context))
    elif command == "!file" or command.startswith("!file "):
        path = command.removeprefix("!file").strip()
        if not path:
            await actor.post("Usage: !file PATH (relative to the workspace).")
            return True
        if path.startswith("`") and path.endswith("`"):
            path = path[1:-1]
        path = await asyncio.to_thread(os.path.expanduser, path)
        try:
            upload = await actor.manager.check_upload(actor.context.workspace, path, explicit=True)
            link = await upload_file(actor.context, upload)
        except t.UploadRefused as exc:
            await actor.post("Upload refused: " + actor.renderer.render_inline(str(exc)))
        else:
            await actor.post(disarm_mentions(link))
    elif command == "!handover":
        if delivery is None:
            await actor.post("Send !handover as a topic message.")
        else:
            await actor.deliver(replace(delivery, text=HANDOVER))
    else:
        return False
    return True


def login_required(kind: str) -> str:
    service = "Claude" if kind == "claude" else "ChatGPT"
    return (
        f"This agent isn't connected to your {service} subscription yet: "
        "send me !login in a direct message."
    )
