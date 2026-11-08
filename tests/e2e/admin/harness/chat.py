"""Talking to an admin bot, and reading what it says (docs/ADMIN.md §2.2, §2.4, §2.8; #356).

A test user (`e2e.harness.zulip_user.TestUser`) sends a command in a bot's channel, in a topic
of the scenario's own, and waits for the bot's answer by pattern, with a timeout. The parsers
below are pure and tested offline; they read what `zab.admin.planning` and the runner post:

- the plan message: `**P-12** · <workshop> · `!launch all` · requested by … · expires …`, its
  targets, its commands (one argument list per line, fenced), its dry run, its status table
  (one line per VM, `status_rows`), and
  `**Plan** `<12 hex>`: approve with `!approve P-12 3f9a2c[ phrase]``; a plan cut to fit
  Zulip's limit attaches the whole plan, which `whole` fetches;
- the answer to an approval: `✅ P-12 approved`, `✅ P-12: 1 of 2 approvals, from distinct
  approvers` (a destructive plan, recorded: `endorse`) or `❌ <reason>`; and the result,
  `✅ **P-12** · `!launch all`: done in …`, `❌ **P-12** …: failed at …` or `⏱ …`;
- a command whose dry run says there is nothing to change (#708): `☑ **P-12** · `!roster sync`:
  nothing to change, as its dry run says; no plan to approve`, then its dry run, and no plan
  (`unchanged_of`, `Talk.unchanged`);
- `!status`'s answer: the plans' status table, then readable lines (`status_of`, #439);
- the approval message, which `Talk.request` waits for and which must show the plan's exact
  approve command (`shows`): its `zform` widget (`widget_replies`), or none for a plan that
  needs a typed phrase, whose text shows the line to send as a one-line code block (#542);
- a script's table (`admin_common.format_table`, `table`);
- the chain head every result shows: `audit #1234 · chain a1b2c3d4e5f6` (§2.8);
- the bot's 👀 on a command it took (`Talk.acknowledged`, #466).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from e2e.harness.zulip_user import TestUser, topic_narrow

#: `footer(head)`: `audit #1234 · chain a1b2c3d4e5f6` (`zab.admin.reads.footer`).
FOOTER = re.compile(r"audit #(\d+) · chain ([0-9a-f]{12})")
PLAN_HEAD = re.compile(r"^\*\*(P-\d+)\*\* · (\S+) · `([^`]*)` · requested by ")
APPROVE = re.compile(r"approve (?:with|by typing) `(!approve (P-\d+) ([0-9a-f]{6})(?: ([^`]+))?)`")
RESULT = re.compile(r"^(✅|❌|⏱) \*\*(P-\d+)\*\* · `")
#: Nothing to change (#708): `planning.UNCHANGED_MARK`, the plan, its command and the words.
UNCHANGED = re.compile(
    r"^☑ \*\*(P-\d+)\*\* · `[^`]*`: nothing to change, as its dry run says; no plan to approve$",
    re.M,
)
LINK = re.compile(r"\]\((/user_uploads/[^)\s]+)\)")
#: The header of a plan's status table (`zab.admin.planning.STATUS_COLUMNS`).
STATUS_HEAD = re.compile(r"^VM {2,}state {2,}zab {2,}idle stop {2,}running turns$", re.M)
#: What a bot answers `!status` with (#439): the plans' table, then readable lines; or `❌`.
STATUS_ANSWER = r"running turns|^❌"
#: A one-VM `!status`'s last error, in its code span (`planning._said`).
LAST_ERROR = re.compile(r"^- last error: (`+) ?(.*?) ?\1$", re.M)
#: Whatever a bot answers a command with, first: a plan, nothing to change, or a refusal.
ANSWER = r"^(\*\*P-\d+\*\* · |☑|❌|⚠|🚫)"
#: How long a bot's read answer may take (its queue, one lookup, the post).
READ_S = 60.0
#: How long a plan may take to come: its dry runs and status read (the runner's deadline).
PLAN_S = 900.0
#: §2.4 step 5's apply timeouts, by verb, and a margin for the result's post: 30 min for the
#: final sync, the export and the teardown (whose retries of a VM refused as protected, #378).
#: A deployer's `!upgrade-server` (#569) has its recheck first, the plan helper again (within
#: `upgrade_server.py`'s 1350 s), then its 1800 s apply (review of #902, N5).
APPLY_S = {"launch": 3700.0, "rollout": 1900.0, "final-sync": 1900.0, "export": 1900.0,
           "teardown": 1900.0, "upgrade-server": 1350.0 + 1800.0 + 100.0}  # fmt: skip
APPLY_DEFAULT_S = 1000.0
#: Beyond an apply's own deadline, for its result's post.
POST_MARGIN_S = 100.0


def apply_s(plan: Plan) -> float:
    """How long a plan's apply may take, its result posted: its verb's deadline; a `!retire`'s
    grows with its rows and `export` (`actions._build_retire`'s, Codex on #566)."""
    words = plan.command.removeprefix("!").split(" ")
    if words[0] != "retire":
        return APPLY_S.get(words[0], APPLY_DEFAULT_S)
    from zab.admin.actions import EXPORT_ROW_S, RETIRE_FLOOR_S, RETIRE_ROW_S

    rows = [w for w in words[1:] if w not in ("export", "unsynced")]
    export = "export" in words[1:]
    deadline = max(RETIRE_FLOOR_S, len(rows) * RETIRE_ROW_S) + export * len(rows) * EXPORT_ROW_S
    return deadline + POST_MARGIN_S


#: The bot's reaction on a command it took (#466): 👀, by its code (`zab.admin.bot.ACK`).
ACK_CODE = "1f440"
#: How soon it must show, from the send, polled every `ACK_EVERY` seconds.
ACK_S, ACK_EVERY = 5.0, 0.5


def heads(text: str) -> list[tuple[int, str]]:
    """Every chain head quoted in `text`, as `(seq, 12 hex)`."""
    return [(int(seq), chain) for seq, chain in FOOTER.findall(text)]


def fenced(text: str, start: str, end: str) -> list[str]:
    """The lines of the fenced block after the line `start`, before the line `end`."""
    lines = text.split("\n")
    try:
        first = next(i for i, ln in enumerate(lines) if ln.startswith(start))
    except StopIteration:
        return []
    out = []
    for line in lines[first + 1 :]:
        if line.startswith(end):
            break
        if not re.fullmatch(r"`{3,}\S*", line):
            out.append(line)
    return out


@dataclass
class Plan:
    """A plan message, as the bot posted it (whole, with its attachment when it was cut)."""

    id: str
    workshop: str
    command: str
    approve: str  # the exact approval command, phrase included
    hash6: str
    text: str
    message_id: int = 0
    targets: list[str] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)
    approval_id: int = 0  # the approval message's, with its widget (`Talk.request`)
    #: the bot's answer to the approval that completed it (`Talk.approve`): `✅ P-n approved`,
    #: and for a plan of several approvers, the rule that completed it (#558)
    answered: str = ""

    @property
    def phrase(self) -> str:
        return self.approve.split(" ", 3)[3] if self.approve.count(" ") >= 3 else ""


def parse_plan(text: str, message_id: int = 0) -> Plan | None:
    head, approve = PLAN_HEAD.search(text), APPROVE.search(text)
    if not head or not approve or approve.group(2) != head.group(1):
        return None
    # the attached whole plan's `Targets (n): …` line when there is one: the message's own
    # `**Targets** (n): a, b, … and 58 more` is cut to fit
    lines = re.findall(r"^(\*{0,2})Targets\*{0,2} \(\d+\): (.*)$", text, re.M)
    found = next((ln for bold, ln in lines if not bold), lines[0][1] if lines else "")
    targets = [t.strip() for t in found.split(",")]
    targets = [t for t in targets if t and t != "none" and not t.startswith("…")]
    commands = fenced(text, "**Commands**", "**Dry run**")
    if "\nCommands, in order:\n" in text:  # the attached whole plan: one command per line
        commands = text.split("\nCommands, in order:\n", 1)[1].split("\nDry run", 1)[0].split("\n")
    return Plan(head.group(1), head.group(2), head.group(3), approve.group(1), approve.group(3),
                text, message_id, targets, commands)  # fmt: skip


def shows(text: str, command: str) -> bool:
    """Whether `text` shows `command` exactly, ready to copy: as a code span (a plan's **Plan**
    line, a no-phrase approval message), or as the one line of a fenced code block (a phrase
    plan's approval message, #542 and the review of #544)."""
    fenced = rf"^(`{{3,}})[^`\n]*\n{re.escape(command)}\n\1$"
    return f"`{command}`" in text or re.search(fenced, text, re.M) is not None


def result_of(text: str) -> tuple[str, str] | None:
    """(mark, plan) of a result message: `✅` done, `❌` failed, `⏱` timed out."""
    found = RESULT.match(text)
    return (found.group(1), found.group(2)) if found else None


def unchanged_of(text: str) -> str | None:
    """The plan ID of an answer that says there is nothing to change (#708), on its first line:
    `☑`, a success, with no plan; None for anything else."""
    first = text.split("\n", 1)[0]
    found = UNCHANGED.match(first)
    return found.group(1) if found else None


class Refused(AssertionError):
    """The bot answered a command with a refusal (`❌`, `⚠`, `🚫`), not a plan."""

    def __init__(self, message: dict[str, Any]) -> None:
        super().__init__(message["content"].split("\n", 1)[0][:300])
        self.message = message


class Unchanged(AssertionError):
    """The bot answered a command with nothing to change (`☑`, #708): no plan, and no refusal
    either. Never a `Refused`, so that a scenario expecting a refusal fails on it."""

    def __init__(self, message: dict[str, Any]) -> None:
        super().__init__(message["content"].split("\n", 1)[0][:300])
        self.message = message


async def attachments(user: TestUser, message: dict[str, Any]) -> list[str]:
    """The texts of the message's attachments (a plan, an output or a status cut to fit)."""
    texts = []
    for path in LINK.findall(message["content"]):
        found = await user.client.request("GET", path.removeprefix("/"))
        data, _ = await user.client.download(found["url"], 2_000_000)
        texts.append(data.decode("utf-8", "replace"))
    return texts


async def whole(user: TestUser, message: dict[str, Any]) -> str:
    """The message's text, then its attachments'."""
    return "\n".join([message["content"], *await attachments(user, message)])


def widget_replies(message: dict[str, Any]) -> list[str] | None:
    """The replies of the `zform` buttons on `message` (its `widget` submessage, as
    `planning.approval_message` sends it): the Approve and Reject commands. None: the message
    has no such widget, as the approval message of a plan that needs a typed phrase has none
    (§2.5, #542)."""
    for sub in message.get("submessages") or []:
        if sub.get("msg_type") != "widget":
            continue
        try:
            content = json.loads(sub.get("content") or "")
        except ValueError:
            continue
        if isinstance(content, dict) and content.get("widget_type") == "zform":
            choices = (content.get("extra_data") or {}).get("choices") or []
            return [str(c.get("reply", "")) for c in choices if isinstance(c, dict)]
    return None


def table(text: str, first: str) -> list[dict[str, str]]:
    """The rows of the first table `admin_common.format_table` printed in `text` whose first
    header is `first`, each as {header: cell}: cut where the separator's dashes start, since a
    cell may hold single spaces. AssertionError when there is no such table."""
    lines = text.splitlines()
    for i, line in enumerate(lines[:-1]):
        rule = lines[i + 1].rstrip()
        if line.split("  ")[0] != first or not re.fullmatch(r"-+(  -+)*", rule):
            continue
        starts = [m.start() for m in re.finditer(r"-+", rule)]
        spans = list(zip(starts, [*starts[1:], None], strict=True))
        heads = [line[a:b].strip() for a, b in spans]
        rows = []
        for body in lines[i + 2 :]:
            if not body.strip() or re.fullmatch(r"`{3,}\S*", body.strip()):
                break
            rows.append({h: body[a:b].strip() for h, (a, b) in zip(heads, spans, strict=True)})
        return rows
    raise AssertionError(f"no table whose first column is {first!r} in: {text[-500:]}")


def status_of(text: str) -> dict[str, dict[str, str]]:
    """slug → its cells in a `!status` answer (`zab.admin.planning.status_reply`, #439): the
    plans' table (`status_rows`), and for one VM, its `- last error: `…`` line as `last
    error`, the code span's text (absent without one)."""
    rows = status_rows(text)
    if len(rows) == 1 and (found := LAST_ERROR.search(text)):
        next(iter(rows.values()))["last error"] = found.group(2)
    return rows


def status_rows(text: str) -> dict[str, dict[str, str]]:
    """slug → its cells (`state`, `zab`, `idle stop`, `running turns`) in the status tables of
    a plan (`zab.admin.planning.status_view`, #361) or of `!status` (#439): the message's table,
    cut to fit, then its attachment's whole one. Columns are two or more spaces apart; `?` is
    unknown."""
    out: dict[str, dict[str, str]] = {}
    for found in STATUS_HEAD.finditer(text):
        columns = re.split(r" {2,}", found.group(0))
        for line in text[found.end() :].split("\n")[1:]:
            cells = re.split(r" {2,}", line.strip())
            if len(cells) != len(columns):
                break
            out[cells[0]] = dict(zip(columns[1:], cells[1:], strict=True))
    return out


@dataclass
class Talk:
    """`user` and one admin bot in the bot's channel, in one topic."""

    user: TestUser
    bot: int
    channel: int
    topic: str

    @property
    def narrow(self) -> list[dict[str, Any]]:
        return topic_narrow(self.channel, self.topic)

    async def say(self, text: str) -> int:
        return await self.user.send(self.channel, self.topic, text)

    async def reply(self, after: int, pattern: str, *, within: float = READ_S) -> dict[str, Any]:
        """The bot's first message after `after` matching `pattern`."""
        return await self.user.wait_reply(
            self.narrow, after_id=after, senders=[self.bot], pattern=pattern, within=within
        )

    async def ask(self, text: str, pattern: str, *, within: float = READ_S) -> dict[str, Any]:
        return await self.reply(await self.say(text), pattern, within=within)

    async def silent(self, after: int, seconds: float) -> list[dict[str, Any]]:
        """What the bot said after `after` within `seconds`: none is a negative test's pass."""
        return await self.user.silent(self.narrow, after_id=after, senders=[self.bot],
                                      seconds=seconds)  # fmt: skip

    async def reacted(self, message_id: int) -> list[str]:
        """The codes of the bot's reactions on the message."""
        message = await self.user.message(message_id)
        return [str(r.get("emoji_code")) for r in message.get("reactions") or []
                if r.get("user_id") == self.bot]  # fmt: skip

    async def acknowledged(self, sent: int, *, within: float = ACK_S) -> float:
        """Seconds from now until the bot's 👀 is on the message `sent` (#466), polled every
        `ACK_EVERY` seconds, a transient error retried; TimeoutError after `within`."""
        clock = self.user.clock
        start = clock()
        while ACK_CODE not in await self.user.retrying(
            lambda: self.reacted(sent), until=start + within, what="the bot's 👀"
        ):
            if clock() - start >= within:
                raise TimeoutError(f"no 👀 from the bot on {sent} within {within:.0f} s")
            await self.user.sleep(ACK_EVERY)
        return clock() - start

    async def request(self, command: str, *, within: float = PLAN_S) -> Plan:
        """`command` sent; its plan, whole, once the bot has posted its approval message too
        (only then is the plan pending: an approval before it is refused, §2.5); `Refused` with
        the bot's answer otherwise. AssertionError when the approval message's text doesn't
        show the plan's exact approve command, phrase included (`shows`, #542)."""
        answer = await self.ask(command, ANSWER, within=within)
        if answer["content"].startswith("☑"):
            raise Unchanged(answer)
        plan = parse_plan(await whole(self.user, answer), answer["id"])
        if plan is None:
            raise Refused(answer)
        approval = await self.reply(answer["id"], rf"^{plan.id} `[^`]*`: approve ")
        plan.approval_id = approval["id"]
        assert shows(approval["content"], plan.approve), (
            f"{plan.id}'s approval message doesn't show `{plan.approve}`: "
            f"{approval['content'][:300]}"
        )
        return plan

    async def unchanged(self, command: str, *, within: float = PLAN_S) -> tuple[str, str]:
        """`command` sent, answered with nothing to change (#708): (its plan ID, the answer
        whole, the dry run under its first line). AssertionError with the answer when the bot
        posts a plan or a refusal instead: either comes first, before any `☑`, since the bot
        answers a command once."""
        answer = await self.ask(command, ANSWER, within=within)
        text = await whole(self.user, answer)
        plan = unchanged_of(text)
        assert plan is not None, f"{command}: not answered as nothing to change: {text[:400]}"
        return plan, text

    async def approval(self, plan: Plan) -> dict[str, Any]:
        """The plan's approval message as GET /messages gives it, with its submessages: the
        widget the web client renders, if any (`widget_replies`)."""
        found = [m for m in await self.user.after(self.narrow, plan.approval_id - 1)
                 if m["id"] == plan.approval_id]  # fmt: skip
        assert found, f"{plan.id}'s approval message {plan.approval_id} not found"
        return found[0]

    async def endorse(self, plan: Plan, text: str | None = None) -> dict[str, Any]:
        """One approval of a plan that needs more (`destructive_approvals`, §2.5), with `text`
        (its own approval command by default): the bot's answer, `✅ P-n: 1 of 2 approvals,
        from distinct approvers`; `Refused` when the approval is, or when it was the last one
        and the plan went on to apply (`approve` is for that one)."""
        answer = await self.ask(text or plan.approve, rf"^(✅ {plan.id}[: ]|❌|busy)")
        if not re.match(rf"✅ {plan.id}: \d+ of \d+ approvals\b", answer["content"]):
            raise Refused(answer)
        return answer

    async def approve(
        self, plan: Plan, text: str | None = None, *, within: float | None = None
    ) -> tuple[dict[str, Any], str]:
        """The plan approved (with `text`, its own approval command by default), then what
        became of it: the result (`✅|❌|⏱ **P-n** · …`), or the runner's word that it did not
        apply (`❌ P-n is stale: …`, `❌ P-n: … nothing was done`), whole. `Refused` when the
        approval itself is."""
        sent = await self.say(text or plan.approve)
        answer = await self.reply(sent, rf"^(✅ {plan.id} |❌|busy)")
        if not answer["content"].startswith(f"✅ {plan.id} "):
            raise Refused(answer)
        plan.answered = answer["content"].split("\n", 1)[0]
        wait = within or apply_s(plan)
        ended = rf"^((✅|❌|⏱) \*\*{plan.id}\*\* · |❌ {plan.id}[ :])"
        done = await self.reply(sent, ended, within=wait)
        return done, await whole(self.user, done)

    async def run(self, command: str, *, phrase: bool = True) -> tuple[Plan, str]:
        """`command` planned, approved as the plan says, and done: (plan, the result's text).
        AssertionError unless every step exited 0."""
        plan = await self.request(command)
        assert phrase or not plan.phrase, f"{plan.id} asks for the phrase {plan.phrase!r}"
        _, text = await self.approve(plan)
        assert text.startswith(f"✅ **{plan.id}**"), f"{plan.id}: {text.split(chr(10))[0]}"
        return plan, text
