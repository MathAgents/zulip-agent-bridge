"""The read commands a bot answers itself (docs/ADMIN.md §2.2): `!help`, `!version`, `!plans`,
`!plan P-n`, `!audit [n]` and `!roster show [slug]`.

Each returns the reply's text; the bot audits a `read` record and adds its chain head (§2.8).
A workshop's bot shows only its own workshop: its plans, its records, its roster. Text from
files (the roster, the audit log) is scrubbed and fenced (`Scrubber.block`, §2.7).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from zab.admin import fleet, monthend
from zab.admin.actions import Fleet
from zab.admin.approvals import Approvals
from zab.admin.audit import Head
from zab.admin.grammar import LATER, SPECS, Command

if TYPE_CHECKING:
    from zab.admin.bot import AdminBot, AdminServer, Incoming

LOG = logging.getLogger("zab.admin.reads")
AUDIT_DEFAULT = 10
#: Kept free in a read's answer for its audit footer (`planning.FOOTER_ROOM`'s size).
FOOTER_ROOM = 64
#: `!roster show`'s columns, and their headings.
COLUMNS = (
    ("slug", "slug"),
    ("name", "name"),
    ("email", "email"),
    ("github", "github"),
    ("region", "region"),
    ("ip", "ip"),
    ("zulip", "zulip"),
    ("repo_status", "repo"),
    ("keep_until", "kept until"),  # a kept VM's end, UTC: !keep-until's (#570)
    ("excluded", "excluded"),  # !roster remove's mark (#627)
)

_UNSAFE_NAME = str.maketrans(dict.fromkeys("*`|[]\\@_<>\n\r\t"))

Read = Callable[["AdminServer", "AdminBot", "Incoming", Command, Fleet | None], Awaitable[str]]


def shown_name(name: str) -> str:
    """A Zulip name as a bot writes it: no markup, no mention, one line, at most 60."""
    return " ".join(name.translate(_UNSAFE_NAME).split())[:60] or "someone"


def mention(user_id: int, name: str) -> str:
    """A silent mention, which notifies nobody (§2.1)."""
    return f"@_**{shown_name(name)}|{user_id}**"


def footer(head: Head) -> str:
    """The chain head a bot's result shows (§2.8): `audit #seq · chain <12 hex>`."""
    return f"`{head}`"


def short(command: str, limit: int = 200) -> str:
    """A command as a message shows it: whole, or its first words within `limit` characters
    and how many it has. A command's line can be as long as a message (Codex on #346)."""
    if len(command) <= limit:
        return command
    words = command.split(" ")
    kept: list[str] = []
    for word in words:
        if len(" ".join([*kept, word])) > limit - 30:
            break
        kept.append(word)
    return " ".join(kept) + f" … ({len(words)} words in all)"


def utc(ts: float | None) -> str:
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ts)) if ts else "-"


def _takes(bot: AdminBot, verb: str, dm: bool) -> bool:
    spec = SPECS[verb]
    here = spec.server or bot.workshop is not None
    return here and (spec.dm or not dm) and not (spec.only_global and bot.workshop is not None)


async def help_(server: AdminServer, bot: AdminBot, m: Incoming, c: Command, _: Any) -> str:
    if c.topic in LATER:
        return f"`!{c.topic}` is not offered through the bots yet; a deployer runs it by hand."
    verbs = [v for v in SPECS if c.topic is None or v.split()[0] == c.topic]
    taken = [v for v in verbs if _takes(bot, v, m.dm)]
    if not taken:  # the server's verbs (`!new-workshop`) go to its bot (review of #424)
        server_verb = any(SPECS[v].only_global for v in verbs)
        whose = "the server's bot" if server_verb else "a workshop's bot"
        return f"`!{c.topic}` is not taken here: send it to {whose}, in its channel."
    where = f"{bot.where}'s bot" if bot.workshop else "the server's bot"
    lines = "\n".join(f"- `{SPECS[v].usage}`" for v in taken)
    # only where a listed verb takes SEL: the server's bot lists none (#435)
    sel = "\nSEL is `all` or slugs of this workshop's roster." * any(
        SPECS[v].selection for v in taken
    )
    return f"**{where}** takes:\n{lines}{sel}"


async def version(server: AdminServer, bot: AdminBot, m: Incoming, c: Command, _: Any) -> str:
    cfg = server.config
    shown = [ws for ws in cfg.workshops if bot.workshop is None or ws is bot.workshop]
    rows = [
        f"- {ws.name}: {ws.aws.region}, {len(ws.organizers)} organizers, its bot "
        f"user {ws.bot.user_id}"
        for ws in shown
    ]
    head = f"zab {server.release} · release `{cfg.release}` · {len(cfg.admins)} global admins"
    return "\n".join([head, *rows])


async def plans(server: AdminServer, bot: AdminBot, m: Incoming, c: Command, _: Any) -> str:
    assert server.store is not None
    pending = server.store.pending(bot.user_id)
    if not pending:
        return "No plan is pending on this bot."
    lines = []
    for plan in pending:
        who = await _name(server, bot, plan.requester)
        lines.append(
            f"- {plan.name} `{short(plan.command)}`, by {who}, expires {utc(plan.expires)}"
        )
    return "\n".join(lines)


async def plan(server: AdminServer, bot: AdminBot, m: Incoming, c: Command, _: Any) -> str:
    assert server.store is not None and c.plan is not None
    found = server.store.get(c.plan)
    if found is None or found.bot != bot.user_id:  # another bot's plans are its own business
        return f"P-{c.plan} is not a plan of this bot."
    who = await _name(server, bot, found.requester)
    approvers = Approvals(server.store, server.config).approved_by(found.id)
    approved = ", ".join([await _name(server, bot, uid) for uid in approvers]) or "nobody yet"
    lines = [
        f"{found.name} `{short(found.command)}`: **{found.state}**"
        + (f" ({found.note})" * bool(found.note)),
        f"requested by {who} at {utc(found.created)}; expires {utc(found.expires)}",
        f"hash `{found.hash[:12]}`" if found.hash else "not posted",
        f"approved by {approved}",
    ]
    return "\n".join(lines)


async def audit(server: AdminServer, bot: AdminBot, m: Incoming, c: Command, _: Any) -> str:
    count = c.count or AUDIT_DEFAULT
    records = await asyncio.to_thread(_tail, Path(server.paths.audit), bot.where, count)
    if not records:
        return "No audit records here yet."
    return server.scrubber.block("\n".join(_brief(r) for r in records)).inline


def _tail(path: Path, workshop: str, count: int) -> list[dict[str, Any]]:
    """The last `count` records: the workshop's own, or every one for the `[global]` bot."""
    kept: deque[dict[str, Any]] = deque(maxlen=count)
    for record in fleet.read_records(path):
        if not workshop or record.get("workshop") == workshop:
            kept.append(record)
    return list(kept)


def _brief(record: dict[str, Any]) -> str:
    words = [f"#{record.get('seq')}", str(record.get("ts")), str(record.get("event"))]
    for key in ("workshop", "plan", "verb"):
        if isinstance(record.get(key), str) and record[key]:
            words.append(record[key])
    for key in ("requester", "sender"):
        if isinstance(record.get(key), dict) and record[key].get("id"):
            words.append(f"by {record[key]['id']}")
    if "exit" in record:
        words.append(f"exit {record['exit']}")
    return " ".join(words)


async def roster(server: AdminServer, bot: AdminBot, m: Incoming, c: Command, _: Any) -> str:
    assert bot.workshop is not None  # the grammar gives `!roster show` to workshop bots only
    rows = await asyncio.to_thread(fleet.read_roster, bot.workshop.roster)
    if c.slug is not None:  # the grammar checked it, on the roster read just before this one
        row = next((r for r in rows if r["slug"] == c.slug), None)
        if row is None:  # an import or edit between the two reads took it out (#347)
            return f"❌ `{c.slug}` is not in the roster any more: `!roster show` lists the rows."
        text = "\n".join(f"{key}: {value}" for key, value in row.items())
        return server.scrubber.block(text).inline
    if not rows:
        how = (
            "`!roster sync`" if bot.workshop.channel else "`!roster import`, with the CSV attached"
        )
        return f"The roster has no participants yet: {how}."
    table = [[_cell(row, key) for key, _ in COLUMNS] for row in rows]
    table.insert(0, [heading for _, heading in COLUMNS])
    widths = [max(len(line[i]) for line in table) for i in range(len(COLUMNS))]
    text = "\n".join(
        "  ".join(v.ljust(w) for v, w in zip(line, widths, strict=True)) for line in table
    )
    block = server.scrubber.block(text)
    more = f"\n{len(rows)} rows: `!roster show <slug>` shows one in full." * block.truncated
    return block.inline + more


def _cell(row: dict[str, str], key: str) -> str:
    if key == "name":  # its Zulip name (`!roster sync`, #627), else a CSV's names
        first_last = " ".join(filter(None, (row.get("first_name"), row.get("last_name"))))
        return row.get("name") or first_last or "-"
    if key == "excluded":
        return "excluded" if row.get("excluded") else "-"
    if key == "zulip":
        return row.get("zulip_user_id") or row.get("zulip_invite") or "-"
    return row.get(key) or "-"


async def month_end(
    server: AdminServer, bot: AdminBot, m: Incoming, c: Command, fl: Fleet | None
) -> str:
    """`!month-end show` (§11.5, #570): the workshop's schedule, from `state.db`. One that
    doesn't fit a message is cut, with the whole of it attached, scrubbed (Codex round 2 on
    #824), as `!status` is; room is kept for the audit footer."""
    assert server.store is not None and fl is not None  # a workshop's verb: its bot has a fleet
    limit = bot.client.limits.get("max_message_length", 10000) - FOOTER_ROOM
    text, whole = monthend.show(server.store.db, fl, limit)
    if whole is None:
        return text
    try:
        url = await bot.client.upload("month-end.txt", server.scrub(whole).encode("utf-8"))
    except Exception as exc:  # its type alone: never the text
        LOG.warning("!month-end show: the attachment failed (%s)", type(exc).__name__)
        return text.replace(monthend.ATTACH, "its attachment failed.")
    return text.replace(monthend.ATTACH, f"[month-end.txt]({url}).")


async def _name(server: AdminServer, bot: AdminBot, user_id: int) -> str:
    user = await server.user(bot, user_id)
    name = user.get("full_name") if user else None
    return mention(user_id, name) if isinstance(name, str) else f"user {user_id}"


READS: dict[str, Read] = {
    "help": help_,
    "version": version,
    "plans": plans,
    "plan": plan,
    "audit": audit,
    "roster show": roster,
    "month-end show": month_end,
}
