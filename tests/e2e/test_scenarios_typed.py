"""Scenario 16 of the end-to-end suite (issue #263): commands as people type them (#709).

The bridge reads a command's verb in any case, a message that is one code span holding a
command, and, in a one-to-one DM, a leading mention of the bot, typed (`@claude`, #713) or
completed (`zab.core.command_text`, SPEC §4.6); `zab sentinel` reads its `!vm` and `!restart`
the same way, so that one of the two answers.

1. **DMs.** `!VM`, `` `!vm` ``, `@**Claude · …** !vm` and `@claude !VM` each get exactly one
   reply: the sentinel's where it is installed, else the bridge's. `!Help`, `` `!status` ``,
   `@**Claude · …** !STATUS` and ``@Codex `!help` `` each get exactly one, the bridge's.
2. **A session topic** (Claude's, started with a mention), under #713's rules: `` `!help` `` and
   `!Usage`, unmentioned, go to both bots and are answered once, by Claude (the VM's first bot,
   and the one with a session here); a prompt that starts with a code span, after a typed
   `@claude` (``@claude `!x` is …``), is delivered to Claude alone, which answers it. No
   "Unknown command" comes, and Codex posts nothing in the topic.
3. **`!login`, for each bot,** started four ways (`!Login`, `` `!login` ``, a mention, a typed
   mention of its kind) and each time cancelled another way (`` `!login cancel` ``,
   `!login  Cancel`, a mention with `!Login cancel`, ``@Claude `!login cancel` ``). Each reply is
   checked: Codex's word for word (`codex.device_code_reply`, its link and code apart), Claude's
   `claude_login.INSTRUCTIONS` and its link. **No sign-in is ever completed.** Both agents must be
   signed in before, by `zab doctor` on the VM (else the part skips: a sign-out would show
   nothing), and still are after; a failure there says so, and the test participant's agents must
   be signed in again by hand (`!login`).

Fast: about 6 minutes, two agent turns and eight sign-ins started and cancelled.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import pytest

from e2e.harness import installed
from e2e.harness.flows import DM_STATUS_RE, Chat, answer, echo, topic_name
from e2e.test_scenarios_features import SENTINEL_VM, check_vm, vm_format
from e2e.test_scenarios_sentinel import DOCTOR, replies, sentinel_up_for
from zab.backends.claude_login import INSTRUCTIONS
from zab.backends.codex import device_code_reply
from zab.core.commands import DM_HELP, HELP

pytestmark = pytest.mark.e2e
#: The sentinel answers a DM sent `CLOCK_MARGIN_S` (5 s) after its queue was registered: one
#: started just now (a deploy) is given this long first, or the bridge would answer too.
SENTINEL_SETTLED_S = 30
CANCELLED = r"^❌ Login failed or cancelled\.$"
#: The answer to a `!login` sent before the last one's clean-up ended (its doctor refresh).
BUSY = "❌ A login is already in progress."
LOGIN_TRIES, LOGIN_PAUSE_S = 12, 5
UNKNOWN = "Unknown command"


def at(name: str) -> str:
    """A mention of the bot as Zulip's typeahead writes it in a DM: `@**Name**`."""
    return f"@**{name}**"


def vm_forms(name: str) -> tuple[str, ...]:
    """To Claude's bot: the last after a typed mention of its kind (#713), then as typed."""
    return ("!VM", "`!vm`", f"{at(name)} !vm", "@claude !VM")


def bridge_forms(name: str) -> tuple[tuple[str, str, str], ...]:
    """(bot kind, what is sent, the bridge's reply as a pattern)."""
    dm_help = "^" + re.escape(DM_HELP.split("\n", 1)[0])
    return (
        ("claude", "!Help", dm_help),
        ("codex", "`!status`", DM_STATUS_RE),
        ("claude", f"{at(name)} !STATUS", DM_STATUS_RE),
        ("codex", "@Codex `!help`", dm_help),
    )


def login_forms(name: str, kind: str) -> tuple[tuple[str, str], ...]:
    """(how a sign-in is started, how it is cancelled), for the bot named `name`, of `kind`:
    the last after a typed mention of its kind (#713), then as typed."""
    return (
        ("!Login", "`!login cancel`"),
        ("`!login`", "!login  Cancel"),
        (f"{at(name)} !login", f"{at(name)} !Login cancel"),
        (f"@{kind} !Login", f"@{kind.capitalize()} `!login cancel`"),
    )


def typed(kind: str, text: str) -> str:
    """`text` after a typed mention of the bot of `kind`, which addresses it alone (#713)."""
    return f"@{kind} {text}"


def topic_help() -> str:
    return "^" + re.escape(HELP.split("\n", 1)[0])


def prompt(token: str) -> str:
    """A prompt that starts with a code span holding `!x`: a prompt, never a command."""
    return f"`!x` is how this topic writes a factorial. {echo(token)}"


def codex_reply() -> str:
    """Codex's `!login` reply, word for word but for its link and code."""
    text = re.escape(device_code_reply("ZABURL", "ZABCODE"))
    return "^" + text.replace("ZABURL", r"https://\S+").replace("ZABCODE", r"\S+") + "$"


def claude_reply() -> str:
    return "^" + re.escape(INSTRUCTIONS) + r"\nhttps://\S+$"


def sign_ins(stdout: str) -> dict[str, bool | None]:
    """Whether each agent is signed in, from `zab doctor --json`: `codex:login`, and every
    `claude account <name>` check; None where the doctor has no such check."""
    try:
        checks = json.loads(stdout)
    except ValueError:
        checks = []
    checks = [c for c in checks if isinstance(c, dict)] if isinstance(checks, list) else []
    codex = [c for c in checks if c.get("name") == "codex:login"]
    claude = [c for c in checks if str(c.get("name", "")).startswith("claude account ")]
    return {
        "claude": all(c.get("ok") is True for c in claude) if claude else None,
        "codex": all(c.get("ok") is True for c in codex) if codex else None,
    }


def doctor(vm) -> dict[str, bool | None]:
    done = vm.run(vm.as_agent("bash", "-lc", DOCTOR), check=False, timeout=300)
    return sign_ins(done.stdout)


async def only_reply(chat: Chat, kind: str, text: str, pattern: str) -> str:
    """The bot's one reply to a DM `text`, which must match `pattern`: the first within two
    minutes, then nothing more within `replies`' quiet time."""
    turn = await chat.dm(kind, text)
    await chat.reply(turn, pattern, within=120)
    said = await replies(chat, turn)
    assert len(said) == 1, f"{kind}, {text!r}: {len(said)} replies: {[s[:80] for s in said]}"
    assert re.search(pattern, said[0]), f"{kind}, {text!r}: {said[0][:300]}"
    return said[0]


async def start_login(chat: Chat, kind: str, text: str, pattern: str) -> dict[str, Any]:
    """The bot's sign-in reply to `text`; sent again while the last sign-in's clean-up runs."""
    either = f"(?:{pattern})|^{re.escape(BUSY)}$"
    for _ in range(LOGIN_TRIES):
        turn = await chat.dm(kind, text)
        msg, _ = await chat.reply(turn, either, within=180)
        if msg["content"] != BUSY:
            return msg
        await asyncio.sleep(LOGIN_PAUSE_S)
    pytest.fail(f"{kind}: still {BUSY!r} after {LOGIN_TRIES} tries of {text!r}")


def require_the_build(vm) -> None:
    if not installed.has(vm, installed.HAS_COMMAND_TEXT):
        pytest.skip("the installed zab reads commands only as typed exactly (#709)")


@pytest.mark.scenario(16)
async def test_dm_commands_as_people_type_them(chat: Chat, vm, scenario):
    require_the_build(vm)
    name = chat.names["claude"]
    sentinel = installed.has(vm, installed.HAS_SENTINEL_UNIT)
    if sentinel:
        for _ in range(12):  # a sentinel just started can't answer yet: the bridge would too
            if sentinel_up_for(vm) >= SENTINEL_SETTLED_S:
                break
            await asyncio.sleep(5)
    fmt = SENTINEL_VM if sentinel else vm_format(vm)
    for text in vm_forms(name):
        check_vm(await only_reply(chat, "claude", text, r"\S"), fmt, dm=True)
    for kind, text, pattern in bridge_forms(name):
        await only_reply(chat, kind, text, pattern)
    scenario.notes.append(
        f"DM: {', '.join(vm_forms('…'))}: one reply each, "
        f"{'the sentinel' if sentinel else 'the bridge'}'s; !Help, `!status`, @… !STATUS: one each"
    )


@pytest.mark.scenario(16)
async def test_topic_commands_as_people_type_them(chat: Chat, vm, scenario, nonce):
    require_the_build(vm)
    topic = topic_name(16, nonce)
    first, later = f"TYPED-{nonce}", f"TYPED2-{nonce}"
    turn = await chat.say("claude", topic, echo(first))
    await chat.reply(turn, answer(first))
    # #713: with no mention, a command goes to both bots, and is answered once: `!help` by the
    # VM's first bot (Claude's sorts first), `!Usage` by the one bot with a session here
    shown = await chat.say("claude", topic, "`!help`", mention_bot=False)
    await chat.reply(shown, topic_help(), within=120)
    usage = await chat.say("claude", topic, "!Usage", mention_bot=False)
    await chat.reply(usage, r"VM: ", within=120)
    # a typed `@claude` addresses Claude alone (#713): a prompt, though it starts with `` `!x` ``
    asked = await chat.say("claude", topic, typed("claude", prompt(later)), mention_bot=False)
    _, took = await chat.reply(asked, answer(later))
    scenario.timing("a prompt that starts with `!x`, answered", took)
    codex = chat.bots["codex"]
    from_codex = [m for m in await chat.user.after(turn.narrow, turn.message_id)
                  if m["sender_id"] == codex]  # fmt: skip
    assert not from_codex, (
        f"Codex answered in Claude's topic: {[m['content'][:80] for m in from_codex]}"
    )
    unknown = [m["content"][:80] for m in await chat.bot_messages(shown) if UNKNOWN in m["content"]]
    assert not unknown, f"a typed command was unknown: {unknown}"
    scenario.notes.append(
        "topic: `!help` and !Usage answered once, unmentioned; an @claude `!x` prompt "
        "answered by Claude alone"
    )


@pytest.mark.scenario(16)
async def test_login_as_people_type_it(chat: Chat, vm, scenario):
    require_the_build(vm)
    before = doctor(vm)
    if before != {"claude": True, "codex": True}:
        pytest.skip(f"not both agents signed in before (zab doctor: {before}): nothing to show")
    patterns = {"claude": claude_reply(), "codex": codex_reply()}
    try:
        for kind in ("claude", "codex"):
            for start, cancel in login_forms(chat.names[kind], kind):
                await start_login(chat, kind, start, patterns[kind])
                stop = await chat.dm(kind, cancel)
                await chat.reply(stop, CANCELLED, within=120)
    finally:
        after = doctor(vm)
    assert after == {"claude": True, "codex": True}, (
        f"an agent is signed out after sign-ins started and cancelled (zab doctor: {after}): "
        "sign the test participant's agents in again with !login"
    )
    scenario.notes.append("!login started 4 ways and cancelled 4 ways, for each bot; still "
                          "signed in")  # fmt: skip
