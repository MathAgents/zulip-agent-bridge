"""The admin bots' event loop (docs/ADMIN.md §2.1, §2.2; #341): startup checks, routing to the
bot's workshop, strangers, the hint, membership, the ledger and the read commands, offline
against a fake realm with two workshops and the `[global]` bot.

`PLACES` and `ADDRESSED` are mutation-tested: each check is removed in turn, and the one case
that only it stops must then get through."""

import asyncio
import dataclasses
import json
import logging
import os
import re
import signal
import threading
from pathlib import Path

import httpx
import pytest

from zab.admin import bot as botmod
from zab.admin import checks, fleet, newworkshop, upgradeserver
from zab.admin.audit import verify
from zab.admin.bot import ADDRESSED, FOREIGN, PLACES, WINDOW, StartupError
from zab.admin.fleet import ApplyEnd, Ledger
from zab.admin.reads import mention
from zab.zulip.client import ZulipClient

from .botkit import (
    ADMINS_CH,
    ALICE,
    CODEX_BOB,
    CODEX_CAROL,
    DEE,
    DREW,
    GLOBAL,
    HARVARD,
    HARVARD_CH,
    HARVARD_ROSTER,
    NOTIFICATION_BOT,
    OLGA,
    OSCAR,
    PAT,
    TESS,
    TEST,
    TEST_CH,
    audit_text,
    incoming,
    make_site,
    until,
)


@pytest.fixture
def site(tmp_path):
    return make_site(tmp_path)


@pytest.fixture
async def server(site):
    srv = site.server()
    await srv.start()
    yield srv
    await srv.close()


def _put(path, text):
    path.write_text(text)


def bot_of(server, user_id):
    return next(b for b in server.bots if b.user_id == user_id)


def replies(site, by=None):
    return [p["content"] for p in site.realm.posts(by)]


def records(site, event=None):
    text = audit_text(site.audit)
    rows = [json.loads(line) for line in text.splitlines()]
    return [r for r in rows if event is None or r["event"] == event]


# ------------------------------------------------------------------------------ routing


async def test_each_bot_serves_its_organizers_and_the_global_admins(site, server):
    harvard, test, glob = (bot_of(server, uid) for uid in (HARVARD, TEST, GLOBAL))
    await harvard.on_message(incoming(OLGA, "!roster show"))
    assert "alice" in replies(site, HARVARD)[-1] and "erin" not in replies(site, HARVARD)[-1]
    await test.on_message(incoming(TESS, "!roster show", stream=TEST_CH))
    assert "erin" in replies(site, TEST)[-1] and "alice" not in replies(site, TEST)[-1]
    # a global admin is served by both workshops' bots, and by the global one
    await harvard.on_message(incoming(DREW, "!roster show"))
    await test.on_message(incoming(DREW, "!roster show", stream=TEST_CH))
    await glob.on_message(incoming(DREW, "!version", stream=ADMINS_CH))
    assert "alice" in replies(site, HARVARD)[-1] and "erin" in replies(site, TEST)[-1]
    assert "harvard-2026: us-west-2, 2 organizers" in replies(site, GLOBAL)[-1]
    # an organizer only by their own workshop's bot
    await test.on_message(incoming(OLGA, "!roster show", stream=TEST_CH))
    assert replies(site, TEST)[-1].startswith("🚫 Olga Organizer is not an organizer here")
    await glob.on_message(incoming(OLGA, "!version", stream=ADMINS_CH))
    assert replies(site, GLOBAL)[-1].startswith("🚫 Olga Organizer is not an organizer here")
    # the deployer is the global bot's, not a workshop's
    await glob.on_message(incoming(DEE, "!help", stream=ADMINS_CH))
    assert "the server's bot" in replies(site, GLOBAL)[-1]
    await harvard.on_message(incoming(DEE, "!help"))
    assert replies(site, HARVARD)[-1].startswith("🚫")
    reads = records(site, "read")
    assert [(r["workshop"], r["verb"], r["requester"]["id"]) for r in reads] == [
        ("harvard-2026", "roster show", OLGA),
        ("zab-test", "roster show", TESS),
        ("harvard-2026", "roster show", DREW),
        ("zab-test", "roster show", DREW),
        ("", "version", DREW),
        ("", "help", DEE),
    ]
    assert records(site, "refused") == []  # one record per sender, bot and window
    await server.flush(every=True)
    assert [(r["bot"], r["sender"]["id"], r["messages"]) for r in records(site, "refused")] == [
        (TEST, OLGA, 1), (GLOBAL, OLGA, 1), (HARVARD, DEE, 1),
    ]  # fmt: skip


async def test_help_explains_sel_only_where_a_listed_verb_takes_it(site, server):
    """#435 item 1: the server's bot takes no verb with SEL, so its help doesn't explain SEL; a
    workshop's bot does, unless what it lists (a topic, or the verbs of a DM) takes none."""
    sel = "SEL is `all` or slugs of this workshop's roster."
    glob, harvard = bot_of(server, GLOBAL), bot_of(server, HARVARD)
    await glob.on_message(incoming(DREW, "!help", stream=ADMINS_CH))
    said = replies(site, GLOBAL)[-1]
    assert said.startswith("**the server's bot** takes:") and "`!new-workshop " in said
    assert "SEL" not in said
    asked = {
        "!help": True,
        "!help status": True,
        "!help start": True,  # `!start SEL|spares`
        "!help roster": False,  # `!roster show [slug]`, `!roster import, …`
        "!help adopt-spare": False,
        "!help version": False,
    }
    for n, (text, explained) in enumerate(asked.items()):
        await harvard.on_message(incoming(OLGA, text, mid=5100 + n))
        said = replies(site, HARVARD)[-1]
        assert said.startswith("**harvard-2026's bot** takes:"), (text, said)
        assert (sel in said) is explained and ("SEL" in said) is explained, (text, said)
    await harvard.on_message(incoming(OLGA, "!help", stream=None, to=(HARVARD,), mid=5200))
    said = replies(site, HARVARD)[-1]  # a DM's verbs: `!help`, `!console`
    assert "`!console`" in said and "`!status SEL`" not in said and "SEL" not in said


async def test_every_reply_carries_its_records_chain_head(site, server):
    await bot_of(server, HARVARD).on_message(incoming(OLGA, "!help"))
    head = verify(site.audit).head
    assert replies(site)[-1].endswith(f"`audit #{head.seq} · chain {head.sha256[:12]}`")


async def test_workshop_commands_are_not_the_global_bots(site, server):
    await bot_of(server, GLOBAL).on_message(incoming(DREW, "!roster show", stream=ADMINS_CH))
    assert "the server's bot" in replies(site, GLOBAL)[-1]
    assert records(site, "read") == []


async def test_a_change_waits_for_the_planner(site, server):
    await bot_of(server, HARVARD).on_message(incoming(OLGA, "!stop alice"))
    assert replies(site)[-1] == "⚠ `!stop` is not available on this server yet"


async def test_a_grammar_error_gets_one_line(site, server):
    await bot_of(server, HARVARD).on_message(incoming(OLGA, "!stop alice --yes"))
    (text,) = replies(site)
    assert text.startswith("❌ ") and "\n" not in text
    assert records(site) == []


# ------------------------------------------------------------------------------ strangers


async def test_a_participants_bot_is_warned_once_per_window(site, server):
    harvard = bot_of(server, HARVARD)
    for n, text in enumerate(("!stop all", "P-1 · fake plan", "!approve P-1 abcdef")):
        await harvard.on_message(incoming(CODEX_BOB, text, mid=6000 + n))
    assert replies(site) == [FOREIGN]
    assert records(site) == []  # one record per window, when it closes
    site.clock.now += WINDOW
    await harvard.on_message(incoming(CODEX_BOB, "!stop all", mid=6010))
    assert replies(site) == [FOREIGN, FOREIGN]
    (first,) = records(site, "foreign")
    assert first["messages"] == 3 and (first["first"], first["last"]) == (6000, 6002)
    assert first["workshop"] == "harvard-2026"
    assert first["sender"] == {"id": CODEX_BOB, "name": "Codex · bob"}
    await server.flush(every=True)
    assert [r["messages"] for r in records(site, "foreign")] == [3, 1]
    assert records(site, "refused") == []  # bots are warned, never answered as people


async def test_the_window_flushes_on_its_own(site, server):
    await bot_of(server, HARVARD).on_message(incoming(CODEX_BOB, "hello"))
    await server.flush()
    assert records(site) == []
    site.clock.now += WINDOW
    await server.flush()
    assert [r["messages"] for r in records(site, "foreign")] == [1]


async def test_a_human_strangers_commands_are_refused_once_per_window(site, server):
    """§2.4: answered once; one `refused` record per sender, bot and window counts them, so
    a flood of DMs can't bloat the audit log and its backups (review of #344)."""
    harvard = bot_of(server, HARVARD)
    for n in range(1000):
        dm = incoming(PAT, "!help", stream=None, to=(HARVARD,), mid=7000 + n)
        await harvard.on_message(dm)
    (answer,) = replies(site)
    assert answer == "🚫 Pat Participant is not an organizer here; nothing was done"
    assert records(site) == []
    await harvard.on_message(incoming(PAT, "just chatting", mid=9000))  # talk: the warning
    assert replies(site)[-1] == FOREIGN
    site.clock.now += WINDOW
    await harvard.on_message(incoming(PAT, "!status all", mid=9001))  # a new window
    assert replies(site)[-1].startswith("🚫 Pat Participant")
    (refused,) = records(site, "refused")
    assert (refused["messages"], refused["first"], refused["last"]) == (1000, 7000, 7999)
    assert refused["sender"] == {"id": PAT, "name": "Pat Participant"}
    await server.flush(every=True)
    assert [r["messages"] for r in records(site, "refused")] == [1000, 1]
    assert [r["messages"] for r in records(site, "foreign")] == [1]


async def test_zulips_system_bots_are_ignored(site, server):
    harvard = bot_of(server, HARVARD)
    await harvard.register()  # `cross_realm_bots` comes with it
    assert server.system_bots == {NOTIFICATION_BOT}
    notice = "@_**Olga Organizer|103** has marked this topic as resolved."
    await harvard.on_message(incoming(NOTIFICATION_BOT, notice))
    await server.flush(every=True)
    assert replies(site) == [] and records(site) == []


async def test_each_strangers_bot_is_warned_in_its_own_window(site, server):
    harvard = bot_of(server, HARVARD)
    await harvard.on_message(incoming(CODEX_BOB, "!stop all", mid=1))
    await harvard.on_message(incoming(CODEX_CAROL, "!stop all", mid=2))
    await bot_of(server, TEST).on_message(incoming(CODEX_BOB, "!stop all", stream=TEST_CH, mid=3))
    assert replies(site) == [FOREIGN] * 3


async def test_a_failed_lookup_is_never_taken_for_a_person(site, server):
    site.realm.users[PAT]["lookup_fails"] = True
    await bot_of(server, HARVARD).on_message(incoming(PAT, "!status all"))
    assert replies(site) == [FOREIGN]  # the warning path: no refusal as for a person


async def test_a_click_on_another_senders_widget_is_not_a_command(site, server):
    harvard = bot_of(server, HARVARD)
    await harvard.on_message(incoming(OLGA, "@**Codex · bob** !approve P-1 abcdef"))
    await harvard.on_message(incoming(OLGA, "@_**Codex · bob** !approve P-1 abcdef"))
    assert replies(site) == [] and records(site) == []


async def test_a_strangers_name_cannot_carry_markup(site, server):
    name = "@**all** `x` [link](http://e)"
    m = botmod.Incoming(5000, PAT, name, HARVARD_CH, "ops", "!status all", frozenset(), frozenset())
    await bot_of(server, HARVARD).on_message(m)
    text = replies(site)[-1]
    assert text.startswith("🚫 all x link(http://e) is not an organizer here")


# ------------------------------------------------------------------------------ the hint


async def test_the_hint_names_only_the_workshop_and_its_bot(site, server):
    test = bot_of(server, TEST)
    await test.on_message(incoming(TESS, "!roster show alice", stream=TEST_CH, mid=1))
    text = replies(site)[-1]
    assert text.startswith("❌ ")
    ask = mention(HARVARD, "Workshop admin · harvard-2026")
    assert f"`alice` is in harvard-2026: ask {ask}" in text
    assert "bob" not in text and "198.51.100" not in text
    # at most one hint per sender every 10 minutes, whatever the command
    await test.on_message(incoming(TESS, "!status alice", stream=TEST_CH, mid=2))
    await test.on_message(incoming(TESS, "!roster show bob", stream=TEST_CH, mid=3))
    assert all("is in harvard-2026" not in t for t in replies(site)[-2:])
    site.clock.now += WINDOW
    await test.on_message(incoming(TESS, "!status bob", stream=TEST_CH, mid=4))
    assert "`bob` is in harvard-2026" in replies(site)[-1]


async def test_the_hint_window_is_the_senders_on_every_bot(site, server):
    await bot_of(server, TEST).on_message(incoming(DREW, "!status alice", stream=TEST_CH))
    assert "`alice` is in harvard-2026" in replies(site, TEST)[-1]
    await bot_of(server, HARVARD).on_message(incoming(DREW, "!status erin", mid=2))
    assert "is in" not in replies(site, HARVARD)[-1]  # the same sender, within 10 minutes


async def test_version_shows_only_the_bots_own_workshop(site, server):
    await bot_of(server, HARVARD).on_message(incoming(OLGA, "!version"))
    assert "harvard-2026: us-west-2" in replies(site)[-1] and "zab-test" not in replies(site)[-1]


async def test_no_hint_for_a_slug_no_workshop_has(site, server):
    await bot_of(server, TEST).on_message(incoming(TESS, "!status zed", stream=TEST_CH))
    assert "is in" not in replies(site)[-1]
    assert server.hints.open == {}  # and no hint was spent


async def test_a_stranger_never_gets_a_hint(site, server):
    await bot_of(server, TEST).on_message(incoming(OLGA, "!status alice", stream=TEST_CH))
    assert replies(site)[-1].startswith("🚫") and "harvard" not in replies(site)[-1]


# ------------------------------------------------------------- a command sent as code (#546)


class Recorder:
    """A planner that records each command that reaches it, and does nothing."""

    def __init__(self):
        self.handled = []

    async def start(self, server):
        pass

    async def stop(self):
        pass

    async def handle(self, bot, m, command, fleet):
        self.handled.append(command.text())

    async def tick(self):
        pass


@pytest.fixture
async def recording(site):
    planner = Recorder()
    srv = site.server(planner=planner)
    await srv.start()
    yield srv, planner
    await srv.close()


#: Each form of a command sent as code: a change, an approval, reads, in a channel.
WRAPPED = [
    "`!stop alice`",  # inline, as a paste of a code span or a one-line code block writes it
    "``!stop all``",
    "```\n!stop all\n```",
    "```text\n!approve P-1 abcdef stop 3\n```",
    "```Text only\n!roster show\n```",  # a pasted ```text block, its newline copied
    "  `!help`\n",
]


async def test_a_command_sent_as_code_gets_the_hint_and_never_runs(site, recording, monkeypatch):
    """Each form gets `AS_CODE`, and nothing else: no plan, no read, no record, no 👀; and the
    grammar only ever reads the message as it was sent, never the text inside the code. The
    same command as plain text then reaches the planner."""
    srv, planner = recording
    harvard = bot_of(srv, HARVARD)
    parsed, real = [], botmod.parse

    def spy(content, ctx, **kw):
        parsed.append(content)
        return real(content, ctx, **kw)

    monkeypatch.setattr(botmod, "parse", spy)
    for n, text in enumerate(WRAPPED):
        await harvard.on_message(incoming(OLGA, text, mid=8000 + n))
    await asyncio.gather(*harvard.acks)
    assert replies(site) == [botmod.AS_CODE] * len(WRAPPED)
    assert planner.handled == [] and srv.store.pending(HARVARD) == []
    assert records(site) == [] and site.realm.reactions == []
    assert parsed == WRAPPED  # once each, as sent
    await harvard.on_message(incoming(OLGA, "!stop alice", mid=8100))
    assert planner.handled == ["!stop alice"]


async def test_the_hint_by_direct_message(site, server):
    harvard = bot_of(server, HARVARD)
    await harvard.on_message(incoming(OLGA, "`!help`", stream=None, to=(HARVARD,)))
    (post,) = site.realm.posts()
    assert post["type"] == "private" and json.loads(post["to"]) == [OLGA]
    assert post["content"] == botmod.AS_CODE
    await harvard.on_message(incoming(OSCAR, "`!help`", stream=None, to=(HARVARD, OLGA)))
    assert len(site.realm.posts()) == 1  # a group DM is never read


async def test_every_command_sent_as_code_is_answered(site, server):
    """No window: a second try sent as code, at once, gets the hint again (silence on it was
    the failure of #546), on every bot, for every organizer; and never a record."""
    harvard, test = bot_of(server, HARVARD), bot_of(server, TEST)
    for n in range(3):
        await harvard.on_message(incoming(OLGA, "`!stop alice`", mid=n + 1))
    await harvard.on_message(incoming(OSCAR, "`!stop bob`", mid=4))
    await test.on_message(incoming(DREW, "`!status erin`", stream=TEST_CH, mid=5))
    await harvard.on_message(incoming(DREW, "`!status alice`", mid=6))
    assert replies(site, HARVARD) == [botmod.AS_CODE] * 5
    assert replies(site, TEST) == [botmod.AS_CODE]
    await server.flush(every=True)
    assert records(site) == []


#: An organizer's code that is not one command: no answer.
CODE_TALK = [
    "`ls`",
    "`!login`",  # a participant bot's command
    "please send `!stop bob`",
    "`!stop bob` then wait",
    "```\n!stop all\n!stop bob\n```",
    "```quote\n!stop all\n```",
    "```python\nprint('!stop all')\n```",
    "> `!stop all`",
]


async def test_other_code_gets_no_answer(site, server):
    harvard = bot_of(server, HARVARD)
    for n, text in enumerate(CODE_TALK):
        await harvard.on_message(incoming(OLGA, text, mid=n + 1))
    assert replies(site) == [] and records(site) == []


async def test_only_those_the_bot_answers_get_the_hint(site, server):
    """A stranger's command sent as code gets what any stranger's talk gets (§2.1): in the
    channel the warning, by DM nothing; and nobody gets the hint where the bot reads nothing."""
    harvard = bot_of(server, HARVARD)
    await harvard.on_message(incoming(PAT, "`!stop all`", mid=1))
    await harvard.on_message(incoming(PAT, "`!help`", stream=None, to=(HARVARD,), mid=2))
    await harvard.on_message(incoming(CODEX_BOB, "`!stop all`", mid=3))
    await harvard.on_message(incoming(TESS, "`!stop all`", mid=4))  # zab-test's organizer
    await harvard.on_message(incoming(OLGA, "`!stop all`", stream=TEST_CH, mid=5))
    await harvard.on_message(incoming(HARVARD, "`!stop all`", mid=6))  # its own
    assert replies(site) == [FOREIGN] * 3


# ------------------------------------------------------------------------------ events


async def test_only_message_events_are_read(site, server):
    harvard = bot_of(server, HARVARD)
    edit = {"type": "update_message", "message_id": 1, "content": "!roster show", "user_id": OLGA}
    await harvard.on_event(edit)
    await harvard.on_event({"type": "message", "message": {"id": "x"}})  # malformed
    assert replies(site) == []
    await harvard.on_event(_rename(HARVARD, "Harvard admin"))
    assert harvard.full_name == "Harvard admin"
    await harvard.on_event(_rename(HARVARD, "a*b"))
    assert harvard.full_name == "Harvard admin"  # a name the grammar can't mention: kept


def _rename(user_id, name):
    return {"type": "realm_user", "op": "update", "person": {"user_id": user_id, "full_name": name}}


async def test_direct_messages(site, server):
    harvard = bot_of(server, HARVARD)
    await harvard.on_message(incoming(OLGA, "!help", stream=None, to=(HARVARD,)))
    assert site.realm.posts()[-1]["type"] == "private"
    assert json.loads(site.realm.posts()[-1]["to"]) == [OLGA]
    await harvard.on_message(incoming(OLGA, "!roster show", stream=None, to=(HARVARD,)))
    assert "only !help and !console work in a direct message" in replies(site)[-1]
    count = len(replies(site))
    await harvard.on_message(incoming(OLGA, "!help", stream=None, to=(HARVARD, OSCAR)))
    await harvard.on_message(incoming(CODEX_BOB, "!help", stream=None, to=(HARVARD,)))
    await harvard.on_message(incoming(PAT, "hello", stream=None, to=(HARVARD,)))
    assert len(replies(site)) == count  # a group DM, a bot's DM, and talk: nothing
    await harvard.on_message(incoming(PAT, "!help", stream=None, to=(HARVARD,)))
    assert replies(site)[-1].startswith("🚫 Pat Participant")


async def test_a_name_only_mention_needs_zulips_flag(site, server):
    harvard = bot_of(server, HARVARD)
    await harvard.on_message(incoming(OLGA, "@**Workshop admin · harvard-2026** !help"))
    assert replies(site) == []
    await harvard.on_message(
        incoming(OLGA, "@**Workshop admin · harvard-2026** !help", flags=("mentioned",))
    )
    await harvard.on_message(incoming(OLGA, "@**Workshop admin · harvard-2026|901** !help"))
    assert len(replies(site)) == 2


@pytest.mark.parametrize("gap", ["  ", "\u00a0", "\t"], ids=["two spaces", "nbsp", "tab"])
@pytest.mark.parametrize("dm", [False, True], ids=["channel", "dm"])
async def test_blanks_after_the_mention_are_one_gap(site, server, gap, dm):
    """#650: Zulip's autocomplete ends a mention with a space, and the sender types another."""
    harvard = bot_of(server, HARVARD)
    where = {"stream": None, "to": (HARVARD,)} if dm else {}
    for named in ("@**Workshop admin · harvard-2026**", "@**Workshop admin · harvard-2026|901**"):
        text = f"{named}{gap}!help"
        await harvard.on_message(incoming(OLGA, text, flags=("mentioned",), **where))
    await harvard.on_message(incoming(OLGA, "!help", **where))
    answers = [text.rpartition("\n")[0] for text in replies(site)]  # less the audit footer
    assert len(answers) == 3 and answers[0] == answers[1] == answers[2]
    assert answers[0].startswith("**harvard-2026's bot** takes:")
    posts = site.realm.posts()  # each answer where the plain `!help`'s went (review of #656)
    assert [p["type"] for p in posts] == ["private" if dm else "stream"] * 3
    assert len({(p["to"], p.get("topic")) for p in posts}) == 1
    if dm:
        assert json.loads(posts[0]["to"]) == [OLGA]
    else:
        assert posts[0]["topic"] == "ops"
    glued = "@**Workshop admin · harvard-2026|901**!help"
    await harvard.on_message(incoming(OLGA, glued, flags=("mentioned",), **where))
    assert replies(site)[-1] == "❌ put a space between the mention and the command"


@pytest.mark.parametrize("gap", ["\u00a0", "\t"], ids=["nbsp", "tab"])
@pytest.mark.parametrize("dm", [False, True], ids=["channel", "dm"])
async def test_a_strangers_command_after_such_a_gap_is_refused(site, server, gap, dm):
    """Review of #656: a human stranger's command after a no-break space or a tab is now a
    command, refused with 🚫 as any of theirs is, where it got ⚠ in a channel and nothing in a
    DM before; a participant bot's still gets the warning in a channel, and nothing in a DM."""
    harvard = bot_of(server, HARVARD)
    where = {"stream": None, "to": (HARVARD,)} if dm else {}
    text = f"@**Workshop admin · harvard-2026**{gap}!help"
    await harvard.on_message(incoming(PAT, text, mid=7000, **where))
    await harvard.on_message(incoming(CODEX_BOB, text, mid=7001, **where))
    refused = "🚫 Pat Participant is not an organizer here; nothing was done"
    assert replies(site) == ([refused] if dm else [refused, FOREIGN])


async def test_other_channels_and_own_messages_are_never_read(site, server):
    harvard = bot_of(server, HARVARD)
    await harvard.on_message(incoming(OLGA, "!help", stream=TEST_CH))
    await harvard.on_message(incoming(HARVARD, "!help"))
    assert replies(site) == []


# ------------------------------------------------------------------- mutation of the checks

#: For each check: a message that only it stops, from whom, and to which bot.
STOPPED_BY = {
    "own": (HARVARD, "!help", {}),
    "channel": (OLGA, "!help", {"stream": TEST_CH}),
    "group-dm": (OLGA, "!help", {"stream": None, "to": (HARVARD, OSCAR)}),
    "unmentioned": (OLGA, "@**Workshop admin · harvard-2026** !help", {}),
}


def test_every_check_has_its_case():
    assert {code for code, _ in PLACES + ADDRESSED} == set(STOPPED_BY)


@pytest.mark.parametrize("code", sorted(STOPPED_BY))
async def test_each_check_is_load_bearing(site, server, monkeypatch, code):
    sender, text, where = STOPPED_BY[code]
    harvard = bot_of(server, HARVARD)
    if sender == HARVARD:  # the bot itself, as if it were an organizer
        harvard.recognized = harvard.recognized | {HARVARD}
    await harvard.on_message(incoming(sender, text, **where))
    assert replies(site) == [], f"{code}: stopped with every check in place"
    for name in ("PLACES", "ADDRESSED"):
        kept = tuple(c for c in getattr(botmod, name) if c[0] != code)
        monkeypatch.setattr(botmod, name, kept)
    await harvard.on_message(incoming(sender, text, **where))
    assert len(replies(site)) == 1, f"{code}: removing it lets nothing through"


# ------------------------------------------------------------------------------ startup


async def test_the_bots_key_must_be_the_configured_bot(site):
    site.realm.me_override[HARVARD] = site.realm.users[HARVARD] | {"is_bot": False}
    with pytest.raises(StartupError, match="its key is not a bot's"):
        await _start(site)
    site.realm.me_override[HARVARD] = site.realm.users[HARVARD] | {"user_id": 999}
    with pytest.raises(StartupError, match="its key is user 999, not 901"):
        await _start(site)


async def test_a_bot_whose_name_cant_be_mentioned_doesnt_start(site):
    site.realm.me_override[TEST] = site.realm.users[TEST] | {"full_name": "admin **bold**"}
    with pytest.raises(StartupError, match="its name can't be mentioned"):
        await _start(site)


async def test_a_malformed_zuliprc_never_shows_its_key(site):
    secret = "k" + "S" * 31
    _put(site.tmp / "etc/secrets/bots/zab-test.zuliprc", f"key={secret}\n")
    with pytest.raises(StartupError, match="unreadable") as raised:
        await _start(site)
    assert secret not in str(raised.value) and "MissingSectionHeaderError" in str(raised.value)


async def test_an_outage_at_start_is_a_startup_error(site):
    def down(request):
        raise httpx.ConnectError("no route to zulip.example.com", request=request)

    srv = site.server(
        connect=lambda path: ZulipClient.from_zuliprc(path, transport=httpx.MockTransport(down))
    )
    try:
        with pytest.raises(StartupError, match=r"GET /users/me failed \(ConnectError\)"):
            await srv.start()
    finally:
        await srv.close()


async def _start(site):
    srv = site.server()
    try:
        await srv.start()
        return srv
    finally:
        await srv.close()


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        (lambda t: (t / "etc/secrets/bots/zab-test.zuliprc").chmod(0o644), "readable by others"),
        (lambda t: (t / "etc/secrets/global.zuliprc").chmod(0o660), "writable by group"),
        (lambda t: (t / "etc/secrets/github-token").chmod(0o604), "readable by others"),
        (lambda t: (t / "etc/admin.toml").chmod(0o664), "writable by group"),
        (lambda t: (t / "etc/secrets").chmod(0o777), "writable by group or others"),
        (lambda t: (t / "etc/break-glass/drew.pub").unlink(), "drew.pub is missing"),
        (lambda t: (t / "etc/secrets/provisioner.zuliprc").unlink(), "is missing"),
        (lambda t: _relink(t / "etc/secrets/bots/harvard-2026.zuliprc"), "symbolic link"),
    ],
)
async def test_strict_modes(site, change, problem):
    change(site.tmp)
    with pytest.raises(StartupError, match=problem):
        await _start(site)
    assert not site.audit.exists()  # nothing started, nothing written


async def test_no_key_is_read_before_the_checks_pass(site):
    (site.tmp / "etc/admin.toml").chmod(0o666)
    opened = []
    srv = site.server(connect=opened.append)
    with pytest.raises(StartupError, match=r"admin\.toml is writable"):
        await srv.start()
    assert opened == []
    await srv.close()


def _relink(path: Path) -> None:
    target = path.with_suffix(".real")
    path.rename(target)
    path.symlink_to(target)


def test_a_linked_directory_is_refused_too(site):
    real = site.tmp / "etc" / "real-secrets"
    (site.tmp / "etc" / "secrets").rename(real)
    (site.tmp / "etc" / "secrets").symlink_to(real)
    problem = checks.check_file(site.tmp / "etc/secrets/global.zuliprc", site.modes, private=True)
    assert problem.endswith("secrets is a symbolic link")


def test_strict_modes_check_owners_and_groups(site):
    zuliprc = site.tmp / "etc/secrets/global.zuliprc"
    stranger = checks.Modes(owners=frozenset({0}), group=site.modes.group, top=site.modes.top)
    assert "is owned by uid" in checks.check_file(zuliprc, stranger, private=True)
    other_group = checks.Modes(site.modes.owners, group=os.getgid() + 1, top=site.modes.top)
    assert "is readable by group" in checks.check_file(zuliprc, other_group, private=True)
    assert checks.check_file(zuliprc, site.modes, private=True) == ""
    assert "is outside" in checks.check_file("/etc/passwd", site.modes)


async def test_a_broken_audit_chain_stops_the_start(site):
    site.audit.write_text('{"seq": 2, "prev": "x"}\n')
    with pytest.raises(StartupError, match="audit verify"):
        await _start(site)


async def test_plan_ids_start_above_the_log_and_the_run_directories(site):
    srv = site.server()
    srv.log.append("request", plan="P-7", verb="stop")
    srv.log.append("refused", plan="P-99", reason="a record that hands out no ID")
    (site.tmp / "state" / "runs" / "P-9").mkdir(parents=True)
    (site.tmp / "state" / "runs" / "notes").mkdir()
    await srv.start()
    try:
        assert srv.store.begin(workshop="zab-test", bot=TEST, requester=TESS, command="!x").id == 10
    finally:
        await srv.close()


DEPLOY = {"ts": "2026-10-20T10:00:00Z", "event": "bootstrap", "version": "v0.3.0",
          "commit": "a" * 40, "tree": "b" * 64, "deployer": "drew"}  # fmt: skip


def _deploys(site, *lines, mode=0o644):
    path = Path(site.paths.deploys)
    path.parent.mkdir(mode=0o755, exist_ok=True)
    path.parent.chmod(0o755)
    path.write_text("".join(f"{line}\n" for line in lines))
    path.chmod(mode)


async def test_deploys_are_copied_into_the_chain_once(site):
    first, wheel = json.dumps(DEPLOY), json.dumps(DEPLOY | {"event": "wheel"})
    _deploys(site, first, wheel)
    srv = await _start(site)
    assert srv.release == "v0.3.0+aaaaaaaaaaaa"
    copied = records(site, "upgrade")
    assert [(r["line"], r["deploy"]["event"]) for r in copied] == [(1, "bootstrap"), (2, "wheel")]
    await _start(site)
    assert len(records(site, "upgrade")) == 2  # a restart copies nothing again
    upgrade = DEPLOY | {"ts": "2026-10-21T10:00:00Z", "event": "upgrade", "version": "v0.3.1"}
    _deploys(site, first, wheel, "", json.dumps(upgrade), "not json")
    srv = await _start(site)
    assert [r["line"] for r in records(site, "upgrade")] == [1, 2, 4, 5]  # blank line 3 skipped
    assert records(site, "upgrade")[3]["deploy"] == {"unreadable": "not json"}
    assert srv.release == "v0.3.1+aaaaaaaaaaaa"
    assert verify(site.audit).ok


async def test_a_restored_server_copies_its_own_deploys(site):
    """§1.7: a replacement server restored from a backup has the old server's lines in its
    chain, and its own deploys.jsonl starts again at line 1 (review of #344)."""
    old = [json.dumps(DEPLOY | {"ts": f"2026-10-2{n}T10:00:00Z"}) for n in range(3)]
    _deploys(site, *old)
    await _start(site)
    assert len(records(site, "upgrade")) == 3
    new = DEPLOY | {"ts": "2026-11-02T09:00:00Z", "version": "v0.3.2", "commit": "c" * 40}
    _deploys(site, json.dumps(new))
    srv = await _start(site)
    assert [r["deploy"]["ts"] for r in records(site, "upgrade")][-1] == "2026-11-02T09:00:00Z"
    assert srv.release == "v0.3.2+cccccccccccc"


async def test_the_config_helpers_changes_are_copied_into_the_chain_once(site):
    """#418, ADMIN §2.9: each line of the root helper's changes.jsonl becomes a `config` record
    at the bot's start (the restart after `!new-workshop`), once, as deploys.jsonl's lines do."""
    path = site.tmp / "helper" / "changes.jsonl"
    path.parent.mkdir(mode=0o755)
    change = {"ts": "2026-10-21T10:00:00Z", "event": "new-workshop", "plan": "P-9",
              "workshop": "mit-2027", "organizers": [103], "bot": 903}  # fmt: skip
    path.write_text(json.dumps(change) + "\n")
    path.chmod(0o644)
    site.paths = dataclasses.replace(site.paths, config_changes=str(path))
    await _start(site)
    (record,) = records(site, "config")
    assert record["change"] == change and record["source"] == "changes.jsonl"
    await _start(site)
    assert len(records(site, "config")) == 1 and records(site, "upgrade") == []
    path.chmod(0o664)  # writable by others: not trusted, not copied
    path.write_text(path.read_text() + json.dumps(change | {"plan": "P-10"}) + "\n")
    await _start(site)
    assert len(records(site, "config")) == 1
    assert verify(site.audit).ok


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
async def test_an_unreadable_change_log_never_stops_the_bots(site, caplog):
    """Codex on #424: a log the bot can't read (the helper's umask, before #421's fix) is said,
    and the bots start."""
    path = site.tmp / "helper" / "changes.jsonl"
    path.parent.mkdir(mode=0o755)
    path.write_text('{"event": "new-workshop", "plan": "P-9"}\n')
    path.chmod(0o600)
    os.chmod(path, 0o000)  # as root's 0600 is to zab-admin
    site.paths = dataclasses.replace(site.paths, config_changes=str(path))
    try:
        await _start(site)
    finally:
        path.chmod(0o644)
    assert records(site, "config") == []
    assert "changes.jsonl not copied" in caplog.text


NESTED = '{"a":' + "[" * 1000 + "]" * 1000 + "}"  # RecursionError in json.loads


@pytest.mark.parametrize("log", ["deploys", "changes"])
async def test_no_line_of_a_trusted_log_stops_the_bots(site, log):
    """Review of #424: a line `json.loads` takes but the audit log can't encode (NaN, Infinity,
    1e999: `allow_nan=False`), one nested past the parser's limit (RecursionError) or past
    `LINE_DEPTH`, is an `unreadable` record, never a failed start; for deploys.jsonl as for the
    helper's changes.jsonl. The good line after them is copied as it is."""
    deep = '{"a":' + '{"b":' * 20 + "1" + "}" * 20 + "}"
    bad = ['{"a": NaN}', '{"a": Infinity}', '{"a": -1e999}', NESTED, deep, "[" * 100_000]
    good = DEPLOY | {"event": "wheel"}
    if log == "deploys":
        _deploys(site, *bad, json.dumps(good))
        event, key = "upgrade", "deploy"
    else:
        path = site.tmp / "helper" / "changes.jsonl"
        path.parent.mkdir(mode=0o755)
        path.write_text("".join(f"{line}\n" for line in [*bad, json.dumps(good)]))
        path.chmod(0o644)
        site.paths = dataclasses.replace(site.paths, config_changes=str(path))
        event, key = "config", "change"
    await _start(site)
    copied = [r[key] for r in records(site, event)]
    assert copied[:-1] == [{"unreadable": line[:2000]} for line in bad]
    assert copied[-1] == good
    assert verify(site.audit).ok


async def test_a_deploys_file_others_can_write_is_not_copied(site):
    _deploys(site, json.dumps(DEPLOY), mode=0o664)
    await _start(site)
    assert records(site, "upgrade") == []


# ------------------------------------------------------------------------------ membership


async def test_membership_warns_about_anyone_unrecognized(site, server):
    site.realm.subscribers[HARVARD_CH] |= {PAT, CODEX_BOB, DEE}
    extra = await server.membership(bot_of(server, HARVARD))
    assert extra == [DEE, PAT, CODEX_BOB]
    post = site.realm.posts(HARVARD)[-1]
    assert post["topic"] == "membership" and "nothing was changed" in post["content"]
    assert mention(PAT, "Pat Participant") in post["content"]
    assert await server.membership(bot_of(server, GLOBAL)) == []  # the deployer is its own
    assert await server.membership(bot_of(server, TEST)) == []
    assert len(site.realm.posts()) == 1


@pytest.mark.parametrize("kind", ["public", "web-public"])
async def test_membership_warns_about_a_channel_that_isnt_private(site, server, kind, caplog):
    """#347 (the review of #344, finding 8): in a public or web-public channel every member of
    the organization reads the plans and `!roster show`, e-mail addresses included, without
    subscribing. The check warns there, and changes nothing; a channel it can't read is
    logged, never warned about."""
    site.realm.privacy[HARVARD_CH] = kind
    assert await server.membership(bot_of(server, HARVARD)) == []  # no stranger subscribed
    (post,) = site.realm.posts(HARVARD)
    assert (post["to"], post["topic"], post["content"]) == (
        str(HARVARD_CH),
        "membership",
        botmod.PUBLIC,
    )
    assert await server.membership(bot_of(server, TEST)) == [] and len(site.realm.posts()) == 1
    del site.realm.subscribers[TEST_CH]  # GET /streams/{id} fails: unknown, not public
    with caplog.at_level(logging.WARNING, logger="zab.admin.bot"):
        await server.membership(bot_of(server, TEST))
    assert len(site.realm.posts()) == 1
    assert f"zab-test bot: channel {TEST_CH} unknown" in caplog.text


async def test_membership_is_checked_at_start_and_hourly(site, server):
    site.realm.subscribers[TEST_CH] |= {PAT}
    await server.memberships()
    await server.memberships()
    assert len(site.realm.posts(TEST)) == 1
    site.clock.now += botmod.MEMBERSHIP
    await server.memberships()
    assert len(site.realm.posts(TEST)) == 2


# ------------------------------------------------------------------------------ scrubbing


async def test_secrets_reload_when_out_changes(site, server):
    key = "Q" * 32
    reply = f"the key is {key}"
    assert key in server.scrub(reply)
    bob = site.tmp / "ws" / "harvard-2026" / "out" / "bob"
    bob.mkdir()
    (bob / "codex.zuliprc").write_text(f"[api]\nkey={key}\n")
    await server.watch_secrets()
    assert key not in server.scrub(reply)
    token = "github_pat_" + "Z" * 40
    assert token not in server.scrub(f"token {token}")  # read at start


async def test_a_key_in_any_zuliprc_form_is_scrubbed_after_the_reload(site, server):
    """#526: a participant's zuliprc written `KEY : …`, its value continued on the next line,
    is learned at the next look, as a `key=` one is."""
    key = "Kx4Wv7Ps2Nq9Rt5" + "Ym8Hb3Lc6Jd1Gf"
    reply = f"the key is {key[:15]} and {key[15:]}"
    assert key[:15] in server.scrub(reply) and key[15:] in server.scrub(reply)
    carol = site.tmp / "ws" / "harvard-2026" / "out" / "carol"
    carol.mkdir()
    (carol / "claude.zuliprc").write_text(f"[api]\nemail: c@x\nKEY : {key[:15]}\n  {key[15:]}\n")
    await server.watch_secrets()
    assert server.scrub(reply) == "the key is [redacted secret] and [redacted secret]"


async def test_a_bot_key_the_helper_adds_is_scrubbed_before_the_restart(site, server):
    """Review of #424: the config helper writes `secrets/bots/<name>.zuliprc` while the bots
    run; the scrubber registers it at its next look, not only after the restart."""
    key = "N" * 32
    reply = f"the new bot's key is {key}"
    assert key in server.scrub(reply)
    added = site.tmp / "etc" / "secrets" / "bots" / "mit-2027.zuliprc"
    added.write_text(f"[api]\nemail=mit-2027-admin-bot@example.zulipchat.com\nkey={key}\n")
    added.chmod(0o640)
    await server.watch_secrets()
    assert key not in server.scrub(reply)


async def test_posts_and_logs_are_scrubbed(site, server, caplog):
    harvard = bot_of(server, HARVARD)
    key = (site.tmp / "etc/secrets/bots/zab-test.zuliprc").read_text().split("key=")[1].strip()
    await harvard.post(HARVARD_CH, "t", f"leak {key} @**all**")
    assert key not in replies(site)[-1] and "@**all**" not in replies(site)[-1]
    handler = logging.Handler()
    seen = []
    handler.emit = lambda record: seen.append(record.getMessage())
    handler.addFilter(botmod._ScrubLogs(server.scrub))
    logger = logging.getLogger("zab.admin.test")
    logger.addHandler(handler)
    try:
        logger.warning("failed with %s", key)
    finally:
        logger.removeHandler(handler)
    assert seen and key not in seen[0]


async def test_audit_appends_in_a_thread(site, server, monkeypatch):
    calls = []
    real = asyncio.to_thread

    async def spy(func, *args, **kwargs):
        calls.append(getattr(func, "__name__", ""))
        return await real(func, *args, **kwargs)

    monkeypatch.setattr(botmod.asyncio, "to_thread", spy)
    await server.audit("read", workshop="zab-test", verb="help")
    assert calls == ["append"]


# ------------------------------------------------------------------------------ reads


async def test_plans_and_plan_show_only_the_bots_own(site, server):
    harvard, test = bot_of(server, HARVARD), bot_of(server, TEST)
    mine = server.store.begin(workshop="harvard-2026", bot=HARVARD, requester=OLGA, command="!x")
    await test.on_message(incoming(TESS, f"!plan P-{mine.id}", stream=TEST_CH))
    assert replies(site, TEST)[-1].startswith(f"P-{mine.id} is not a plan of this bot")
    await harvard.on_message(incoming(OLGA, f"!plan P-{mine.id}"))
    assert "**planning**" in replies(site, HARVARD)[-1]
    await harvard.on_message(incoming(OLGA, "!plans"))
    assert replies(site, HARVARD)[-1].startswith("No plan is pending")


async def test_audit_shows_the_workshops_own_records(site, server):
    await server.audit("read", workshop="zab-test", verb="secret-verb")
    await bot_of(server, HARVARD).on_message(incoming(OLGA, "!audit 5"))
    assert "secret-verb" not in replies(site)[-1]
    await bot_of(server, GLOBAL).on_message(incoming(DREW, "!audit", stream=ADMINS_CH))
    assert "secret-verb" in replies(site)[-1]


async def test_roster_show_one_row(site, server):
    await bot_of(server, HARVARD).on_message(incoming(OLGA, "!roster show bob"))
    text = replies(site)[-1]
    assert "email: bob@example.com" in text and "alice" not in text


async def test_roster_show_of_a_row_taken_out_meanwhile_is_answered(site, server, monkeypatch):
    """#347 (finding 10c): the grammar checks the slug on one read of the roster, the answer
    reads it again; a row an import took out in between is answered, never a StopIteration
    that leaves the organizer without a reply."""
    harvard = bot_of(server, HARVARD)
    roster, read = Path(harvard.workshop.roster), server.read_fleet
    lines = HARVARD_ROSTER.split("\n")

    async def then_edited(ws):
        fl = await read(ws)  # what the grammar checks the slug against
        _put(roster, "\n".join(line for line in lines if not line.startswith("bob,")))
        return fl

    monkeypatch.setattr(server, "read_fleet", then_edited)
    await harvard.on_message(incoming(OLGA, "!roster show bob"))
    text = replies(site)[-1]
    assert text.startswith("❌ `bob` is not in the roster any more: `!roster show` lists the rows.")
    assert records(site, "read")[-1]["verb"] == "roster show"


async def test_only_the_last_minutes_failed_lookups_are_kept(site, server):
    """The review of #505 (finding 1e): a failure older than `LOOKUP_RETRY` is dropped when
    another lookup fails, so a flood of senders can't grow the table for good."""
    harvard = bot_of(server, HARVARD)
    for uid in (PAT, DEE):
        site.realm.users[uid]["lookup_fails"] = True
    await server.user(harvard, PAT)
    site.clock.now += botmod.LOOKUP_RETRY + 1
    await server.user(harvard, DEE)
    assert set(server.lookups_failed) == {DEE}


async def test_a_failed_lookup_is_remembered_for_a_minute(site, server):
    """#347 (finding 10d): a sender whose `GET /users/{id}` fails costs one request a minute,
    not one per message, against the bot's rate limit."""
    site.realm.users[PAT]["lookup_fails"] = True
    harvard = bot_of(server, HARVARD)

    def asked():
        return site.realm.requests.count((HARVARD, "GET", f"users/{PAT}"))

    for n in range(5):
        await harvard.on_message(incoming(PAT, "hello", mid=8000 + n))
    assert asked() == 1 and replies(site) == [FOREIGN]
    site.clock.now += botmod.LOOKUP_RETRY - 1
    await harvard.on_message(incoming(PAT, "hello", mid=8010))
    assert asked() == 1
    site.clock.now += 1
    del site.realm.users[PAT]["lookup_fails"]
    await harvard.on_message(incoming(PAT, "hello", mid=8011))
    assert asked() == 2
    await harvard.on_message(incoming(PAT, "hello", mid=8012))
    assert asked() == 2  # found: cached for the window, as before


# ------------------------------------------------------------------------------ the ledger


def _end(verb, args, exit=0, ws="harvard-2026"):
    return {"event": "apply_end", **ApplyEnd(ws, "P-3", verb, tuple(args), exit).fields()}


async def test_the_fleet_is_read_from_a_copy_of_the_ledger(site, server, monkeypatch):
    """#347 (finding 10e): `read_fleet` reads in a thread while the event loop may take a
    record into the ledger. The thread reads a copy, taken on the loop when the read began:
    a `!rollout` that starts meanwhile (and forgets alice's uninstall) is not half seen."""
    ws = server.config.workshop("harvard-2026")
    fields = {"workshop": ws.name, "plan": "P-3", "targets": ["alice"]}
    server.ledger.observe({"event": "apply_end", "verb": "uninstall", "exit": 0, **fields})
    reading, go = threading.Event(), threading.Event()
    real = fleet.read_fleet

    def slow(*args, **kwargs):
        reading.set()
        assert go.wait(5)
        return real(*args, **kwargs)

    monkeypatch.setattr(fleet, "read_fleet", slow)
    task = asyncio.create_task(server.read_fleet(ws))
    assert await asyncio.to_thread(reading.wait, 5)
    server.ledger.observe({"event": "apply_start", "verb": "rollout", **fields, "plan": "P-4"})
    assert "alice" not in server.ledger.workshop(ws.name).ending()  # the loop's own, changed
    go.set()
    read = await task
    assert read.ending["alice"].uninstalled.startswith("P-3")  # the copy, as it was


def test_the_ledger_records_what_adopt_spare_replays(site):
    srv = site.server()
    ws = srv.config.workshop("harvard-2026")
    ledger = Ledger()
    assert fleet.read_fleet(srv.config, ws, ledger).settings is None  # nothing recorded
    ledger.observe(_end("provision zulip", ["hold", "all"]))
    ledger.observe(_end("rollout", ["v0.2.2", "all", "lean-shared", "restart-codex", "steer=off"]))
    ledger.observe(_end("rollout", ["v0.2.3", "all"], exit=1))  # failed: not recorded
    ledger.observe(_end("idle-stop", ["30", "all"]))
    got = fleet.read_fleet(srv.config, ws, ledger)
    assert (got.held, got.release, got.settings) == (
        True, "v0.2.2", ("steer=off", "lean-shared", "idle-stop=30"),
    )  # fmt: skip


def test_the_ledger_records_whether_the_fleets_waker_is_on(site):
    """#440: `!adopt-spare` opts the new VM in to the waker when the last successful `!waker` of
    the whole fleet was `on`; one row's, or a failed one, changes nothing."""
    srv = site.server()
    ws = srv.config.workshop("harvard-2026")
    ledger = Ledger()
    ledger.observe(_end("provision zulip", ["all"]))
    ledger.observe(_end("rollout", ["v0.2.2", "all", "idle-stop=60"]))
    assert fleet.read_fleet(srv.config, ws, ledger).waker is False  # never turned on
    ledger.observe(_end("waker", ["on", "all"]))
    ledger.observe(_end("waker", ["off", "bob"]))  # one row: the fleet's stays on
    ledger.observe(_end("waker", ["off", "all"], exit=1))  # failed: not recorded
    assert fleet.read_fleet(srv.config, ws, ledger).waker is True
    ledger.observe(_end("waker", ["off", "all"]))
    assert fleet.read_fleet(srv.config, ws, ledger).waker is False
    ledger.observe(_end("waker", ["on", "all"], ws="zab-test"))  # another workshop's
    assert fleet.read_fleet(srv.config, ws, ledger).waker is False


def test_only_the_whole_fleets_applies_are_recorded(site):
    """The decision on #344: a canary, one VM's change or a late joiner never
    changes what !adopt-spare gives a spare."""
    srv = site.server()
    ws = srv.config.workshop("harvard-2026")
    ledger = Ledger()
    ledger.observe(_end("provision zulip", ["hold", "all"]))
    ledger.observe(_end("rollout", ["v0.2.2", "all", "idle-stop=30"]))
    before = fleet.read_fleet(srv.config, ws, ledger)
    ledger.observe(_end("idle-stop", ["0", "bob"]))
    ledger.observe(_end("rollout", ["v0.2.3", "alice", "steer=off"]))
    ledger.observe(_end("provision zulip", ["carol"]))
    after = fleet.read_fleet(srv.config, ws, ledger)
    assert (after.held, after.release, after.settings) == (True, "v0.2.2", ("idle-stop=30",))
    assert (before.held, before.release, before.settings) == (
        after.held, after.release, after.settings,
    )  # fmt: skip
    other = fleet.read_fleet(srv.config, srv.config.workshop("zab-test"), ledger)
    assert other.settings is None


def test_a_rollout_keeps_what_push_config_keeps(site):
    """Codex round 1 on #344: an omitted --idle-stop leaves the VMs' timer, and nothing
    uninstalls --lean-shared; steer, multi-party and raw-log go back to their defaults."""
    srv = site.server()
    ws = srv.config.workshop("harvard-2026")
    ledger = Ledger()
    ledger.observe(_end("provision zulip", ["all"]))
    ledger.observe(_end("idle-stop", ["45", "all"]))  # before any rollout: kept for later
    ledger.observe(_end("rollout", ["v0.2.2", "all", "lean-shared", "steer=off", "raw-log=meta"]))
    assert fleet.read_fleet(srv.config, ws, ledger).settings == (
        "steer=off", "raw-log=meta", "idle-stop=45", "lean-shared",
    )  # fmt: skip
    ledger.observe(_end("rollout", ["v0.2.3", "all", "idle-stop=60"]))
    got = fleet.read_fleet(srv.config, ws, ledger)
    assert (got.release, got.settings) == ("v0.2.3", ("idle-stop=60", "lean-shared"))
    ledger.observe(_end("rollout", ["v0.2.3", "all", "idle-stop=0", "multi-party=off"]))
    assert fleet.read_fleet(srv.config, ws, ledger).settings == (
        "multi-party=off", "idle-stop=0", "lean-shared",
    )  # fmt: skip


@pytest.mark.parametrize(
    "bad",
    [
        {"event": "apply_end", "workshop": "harvard-2026", "verb": "rollout", "exit": 0},
        _end("rollout", ["v0.2.2", "all"]) | {"exit": "0"},
        _end("rollout", ["v0.2.2", "all"]) | {"args": "v0.2.2 all"},
        _end("rollout", ["v9.9", "all"]),  # not a release this server takes
        _end("rollout", ["v0.2.2", "all", "steer=maybe"]),  # not a setting _replay maps
    ],
)
def test_a_record_that_doesnt_read_back_leaves_it_unrecorded(site, bad):
    srv = site.server()
    ledger = Ledger()
    ledger.observe(_end("provision zulip", ["all"]))
    ledger.observe(bad)
    got = fleet.read_fleet(srv.config, srv.config.workshop("harvard-2026"), ledger)
    assert (got.held, got.release, got.settings) == (None, None, None)


def test_spares_are_read_as_power_py_selects_them(site):
    srv = site.server()
    ws = srv.config.workshop("harvard-2026")
    spares = {
        "harvard-2026/us-west-2/zab-spare-2": {"name": "zab-spare-2", "ip": "192.0.2.2"},
        "harvard-2026/us-west-2/zab-spare-1": {"name": "zab-spare-1", "ip": "192.0.2.1"},
        "harvard-2026/us-west-2/zab-spare-3": {"ip": "192.0.2.3", "adopted_by": "dave"},
        "harvard-2026/us-west-2/zab-spare-4": {"ip": "198.51.100.1"},  # alice's now
        "harvard-2026/us-east-1/zab-spare-5": {"ip": "192.0.2.5"},  # another region
        "zab-test/us-west-2/zab-spare-6": {"ip": "192.0.2.6"},  # another workshop
        "harvard-2026/us-west-2/zab-spare-7": {"ip": ""},  # no address yet
        "harvard-2026/us-west-2/bad name": {"ip": "192.0.2.8"},
    }
    (Path(ws.out_dir) / "spares.json").write_text(json.dumps(spares))
    assert fleet.read_fleet(srv.config, ws).spares == ("zab-spare-1", "zab-spare-2")
    (Path(ws.out_dir) / "spares.json").write_text("[]")
    with pytest.raises(fleet.FleetError, match="not a JSON object"):
        fleet.read_fleet(srv.config, ws)


def test_the_roster_reads_as_the_scripts_read_it(site):
    srv = site.server()
    ws = srv.config.workshop("zab-test")
    Path(ws.roster).write_text("\ufeffSlug,GitHub,IP\n,Erin-GH,203.0.113.5\nfrank,,\n,,\n")
    got = fleet.read_fleet(srv.config, ws)
    assert got.slugs == ("erin-gh", "frank") and got.rows[0].ip == "203.0.113.5"
    Path(ws.roster).write_text("slug\nall\n")
    with pytest.raises(fleet.FleetError, match="not usable in commands"):
        fleet.read_fleet(srv.config, ws)


async def test_an_unreadable_roster_refuses_the_command(site, server):
    _put(Path(server.config.workshop("harvard-2026").roster), "slug\nall\n")
    await bot_of(server, HARVARD).on_message(incoming(OLGA, "!roster show"))
    assert replies(site)[-1].startswith("⚠ the roster of harvard-2026")
    await bot_of(server, HARVARD).on_message(incoming(OLGA, "hello there"))
    assert len(replies(site)) == 1  # talk doesn't read the roster


# ------------------------------------------------------------------------------ selfcheck


def test_selfcheck_scripts_are_the_actions_scripts():
    source = Path(fleet.__file__).with_name("actions.py").read_text()
    found = set(re.findall(r'run\(\s*"([a-z_/]+\.py)"', source)) | {
        newworkshop.SCRIPT,
        upgradeserver.SCRIPT,
    }
    assert found == set(checks.SCRIPTS)


def _stub_release(root):
    for script in checks.SCRIPTS:
        path = root / "workshop" / script
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("import sys\nsys.exit(0 if sys.argv[1:] == ['--help'] else 3)\n")
    return root


def test_the_selfcheck_checks_what_zab_build_can(site, tmp_path, monkeypatch, capsys):
    """The contract (on #344): `selfcheck --config` runs as zab-build, before the
    secrets exist: the strict load, the imports, the scripts' `--help`. It reads no secret and
    checks no owner or mode (here, the zuliprcs it names don't exist)."""
    from zab.admin import starter
    from zab.admin.__main__ import main

    config = tmp_path / "admin.toml"
    text = site.config_path.read_text().replace(str(site.tmp / "etc"), "/etc/zab-admin")
    config.write_text(text.replace(str(site.tmp / "ws"), "/var/lib/zab-admin/ws"))
    monkeypatch.setattr(starter, "sdk", lambda: (None, None))
    assert main(["selfcheck", "--config", str(config)]) == 0  # this tree's real scripts
    assert capsys.readouterr().out.strip() == "selfcheck: OK"
    release = _stub_release(tmp_path / "release")
    monkeypatch.setattr(checks, "release_root", lambda: release)
    (release / "workshop" / "aws" / "power.py").write_text("import boto3_missing\n")
    assert main(["selfcheck", "--config", str(config)]) == 1
    assert "selfcheck: aws/power.py --help exited 1" in capsys.readouterr().out


def test_the_selfcheck_fails_in_a_venv_without_the_bots_imports(site, tmp_path, monkeypatch):
    """#347 (the approval of #344): selfcheck imports the bot for what it imports (httpx, the
    Zulip client), so that a release whose venv lacks one fails before the switch. With httpx
    hidden and the bot not yet imported, it fails; dropping that import would let it pass."""
    import sys

    import zab.admin
    from zab.admin import starter
    from zab.admin.__main__ import main

    config = tmp_path / "admin.toml"
    text = site.config_path.read_text().replace(str(site.tmp / "etc"), "/etc/zab-admin")
    config.write_text(text.replace(str(site.tmp / "ws"), "/var/lib/zab-admin/ws"))
    monkeypatch.setattr(starter, "sdk", lambda: (None, None))
    monkeypatch.setattr(checks, "release_root", lambda: _stub_release(tmp_path / "release"))
    monkeypatch.setitem(sys.modules, "httpx", None)  # `import httpx` raises ImportError
    monkeypatch.delitem(sys.modules, "zab.admin.bot")  # imported afresh, as in a new process
    monkeypatch.delattr(zab.admin, "bot")
    with pytest.raises(ImportError, match="httpx"):
        main(["selfcheck", "--config", str(config)])


def test_the_selfcheck_runs_the_config_helpers_command(tmp_path):
    """#437: the root helper's command, as its unit gives it, from the release: the system
    interpreter under -I -B -S. A helper that imports from the venv fails it, as do a release
    without that unit and a unit that runs the venv's interpreter, which zab-build made (review
    of #438). It writes nothing into the tree."""
    from zab.admin.__main__ import HELPER_PYTHON, HELPER_UNIT, check_helper

    assert check_helper(checks.release_root()) == []
    release = tmp_path / "release"
    assert check_helper(release) == [f"{HELPER_UNIT}: No such file or directory"]
    unit = release / HELPER_UNIT
    unit.parent.mkdir(parents=True)
    unit.write_bytes((checks.release_root() / HELPER_UNIT).read_bytes())
    package = release / "src" / "zab" / "admin"
    package.mkdir(parents=True)
    for init in (package.parent / "__init__.py", package / "__init__.py"):
        init.write_text("")
    (package / "confighelper.py").write_text("import httpx\n\ndef main():\n    return 0\n")
    (problem,) = check_helper(release)
    assert problem.startswith("the config helper's command exited 1: ModuleNotFoundError")
    assert not list(release.rglob("__pycache__"))
    assert HELPER_PYTHON == "/usr/bin/python3"
    text = unit.read_text().replace(
        f"={HELPER_PYTHON} ", "=/opt/zab-admin/current/.venv/bin/python "
    )
    unit.write_text(text)
    assert check_helper(release) == [f"{HELPER_UNIT}: not one ExecStart of /usr/bin/python3"]


async def test_bot_check_runs_the_startup_checks_alone(site, capsys):
    """`python -m zab.admin bot --check`: the owners and modes, the keys and `users/me`, for an
    operator to run as zab-admin; nothing else starts and nothing is written."""
    assert await botmod.check(site.server()) == 0
    assert capsys.readouterr().out.strip() == "bot check: OK (3 bots)"
    _remove(site.tmp / "etc/break-glass/drew.pub")
    assert await botmod.check(site.server()) == 1
    assert "drew.pub is missing" in capsys.readouterr().out
    assert not _exists(site.audit) and not _exists(Path(site.paths.state_db))


def _remove(path):
    path.unlink()


def _exists(path):
    return path.exists()


def test_selfcheck_writes_nothing_into_the_tree(tmp_path):
    """deploy.sh checks the built tree file by file after selfcheck (#338): no .pyc."""
    root = tmp_path / "release"
    for script in checks.SCRIPTS:
        path = root / "workshop" / script
        path.parent.mkdir(parents=True, exist_ok=True)
        path.with_name("sibling_common.py").write_text("X = 1\n")
        path.write_text("import sibling_common\n")
    assert checks.check_scripts(root) == []
    assert not list(root.rglob("__pycache__")) and not list(root.rglob("*.pyc"))


def test_the_command_line_names_its_commands(capsys):
    from zab.admin.__main__ import main

    assert main([]) == 2
    usage = capsys.readouterr().err
    assert all(
        f"python -m zab.admin {c}" in usage for c in ("audit", "starter", "bot", "selfcheck")
    )


# ------------------------------------------------------------------------------ the queues


async def test_end_to_end_queues_no_backlog_and_requeue(site):
    site.realm.say(OLGA, HARVARD_CH, "!help")  # before the start: never read
    srv = site.server()
    task = asyncio.create_task(srv.run())
    try:
        await until(lambda: len({q.owner for q in site.realm.queues.values()}) == 3)
        assert all(q.event_types == ["message", "realm_user"] for q in site.realm.queues.values())
        site.realm.say(ALICE, TEST_CH, "!roster show")
        site.realm.say(OLGA, HARVARD_CH, "!roster show")
        await until(lambda: len(replies(site)) == 2)
        assert {p["to"] for p in site.realm.posts()} == {str(HARVARD_CH), str(TEST_CH)}
        site.realm.queues.clear()  # Zulip dropped every queue
        site.realm.say(OLGA, HARVARD_CH, "!help")  # lost with them
        await until(lambda: len({q.owner for q in site.realm.queues.values()}) == 3)
        site.realm.say(OLGA, HARVARD_CH, "!version")
        await until(lambda: len(replies(site)) == 3)
        assert replies(site)[-1].startswith("zab ")
        site.realm.say(CODEX_BOB, HARVARD_CH, "!stop all")
        await until(lambda: replies(site)[-1] == FOREIGN)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert [r["messages"] for r in records(site, "foreign")] == [1]  # flushed at the stop


async def test_sigterm_writes_the_open_windows_records(site):
    """systemd stops the bot with SIGTERM: the open warning windows still get their records
    (review of #344), with a real signal to this process."""
    srv = site.server()
    task = asyncio.create_task(botmod.serve(srv))
    await until(lambda: len({q.owner for q in site.realm.queues.values()}) == 3)
    site.realm.say(CODEX_BOB, HARVARD_CH, "!stop all")
    await until(lambda: replies(site) == [FOREIGN])
    os.kill(os.getpid(), signal.SIGTERM)
    await asyncio.wait_for(task, 5)  # serve returns: the signal stopped it, nothing failed
    assert [r["messages"] for r in records(site, "foreign")] == [1]
    assert signal.getsignal(signal.SIGTERM) == signal.SIG_DFL  # the handler is gone


async def test_a_second_signal_never_cuts_the_shutdown_flush(site, monkeypatch):
    """#347 (the re-review of #344): a second SIGTERM, or a SIGINT, while the flush the first
    one started is writing must not cancel it: every open window's record is written, and the
    server is closed."""
    srv = site.server()
    task = asyncio.create_task(botmod.serve(srv))
    await until(lambda: len({q.owner for q in site.realm.queues.values()}) == 3)
    site.realm.say(CODEX_BOB, HARVARD_CH, "!stop all")
    site.realm.say(CODEX_CAROL, HARVARD_CH, "!stop all")
    await until(lambda: replies(site) == [FOREIGN, FOREIGN])
    writing, go = threading.Event(), threading.Event()
    append = srv.log.append

    def slow(event, **fields):
        if event == "foreign" and not writing.is_set():
            writing.set()
            assert go.wait(5)
        return append(event, **fields)

    monkeypatch.setattr(srv.log, "append", slow)
    os.kill(os.getpid(), signal.SIGTERM)
    assert await asyncio.to_thread(writing.wait, 5)  # the flush's first append, in its thread
    os.kill(os.getpid(), signal.SIGINT)
    await asyncio.sleep(0.1)  # the handler has run
    go.set()
    await asyncio.wait_for(task, 5)
    assert sorted(r["sender"]["id"] for r in records(site, "foreign")) == [CODEX_BOB, CODEX_CAROL]
    assert srv.store is None  # closed
    assert verify(site.audit).problem is None


async def test_a_record_that_fails_leaves_its_window_and_the_rest_open(site, server, monkeypatch):
    """#347 (the re-review of #344): the flush closes each window as it writes its record.
    One whose record fails stays open, and so do those after it: the next flush writes
    them, none lost, none twice."""
    harvard = bot_of(server, HARVARD)
    await harvard.on_message(incoming(CODEX_BOB, "hello", mid=1))
    await harvard.on_message(incoming(CODEX_CAROL, "hello", mid=2))
    audit, failed = server.audit, []

    async def once(event, **fields):
        if event == "foreign" and not failed:
            failed.append(fields["sender"]["id"])
            raise OSError("disk full")
        return await audit(event, **fields)

    monkeypatch.setattr(server, "audit", once)
    with pytest.raises(OSError):
        await server.flush(every=True)
    assert failed == [CODEX_BOB] and records(site, "foreign") == []
    assert len(server.foreign.open) == 2  # the failed one, and the one after it
    await server.flush(every=True)
    written = sorted(r["sender"]["id"] for r in records(site, "foreign"))
    assert written == [CODEX_BOB, CODEX_CAROL] and server.foreign.open == {}


def _foreign(site):
    return [
        (r["sender"]["id"], r["messages"], r["first"], r["last"]) for r in records(site, "foreign")
    ]


async def test_a_failed_record_merges_into_the_senders_new_window(site, server, monkeypatch):
    """The review of #505 (finding 2): Bob posts while his window's record is failing, which
    opens a new window. The failed one, reopened, merges into it: the next record counts
    all his messages, from his first, and the window keeps the failed one's start, which
    decides when it is written and when Bob is warned again (the review's note 3)."""
    harvard = bot_of(server, HARVARD)
    began = site.clock.now
    for mid in (1, 2):
        await harvard.on_message(incoming(CODEX_BOB, "hello", mid=mid))
    site.clock.now += 60  # the new window starts a minute later
    audit, failed = server.audit, []

    async def failing(event, **fields):
        if event == "foreign" and not failed:
            failed.append(True)
            await harvard.on_message(incoming(CODEX_BOB, "hello", mid=3))  # during the append
            raise OSError("disk full")
        return await audit(event, **fields)

    monkeypatch.setattr(server, "audit", failing)
    with pytest.raises(OSError):
        await server.flush(every=True)
    assert server.foreign.open[(HARVARD, CODEX_BOB)].start == began
    await server.flush(every=True)
    assert _foreign(site) == [(CODEX_BOB, 3, 1, 3)]


async def test_a_record_that_fails_at_rollover_keeps_its_window_and_answers(
    site, server, monkeypatch, caplog
):
    """#510 (#347's comment 6259596353): Bob posts after his window is over, so `take` closes
    it and `stranger()` writes its record, which fails. The window is opened again, merged into
    the new one; the failure is logged by its type alone; the new message is still answered;
    and the flush once the new window is over writes one record that counts both windows."""
    harvard = bot_of(server, HARVARD)
    for mid in (1, 2):
        await harvard.on_message(incoming(CODEX_BOB, "hello", mid=mid))
    site.clock.now += WINDOW
    audit, failed = server.audit, []

    async def once(event, **fields):
        if event == "foreign" and not failed:
            failed.append(fields["messages"])
            raise OSError("disk full at /var/lib/zab-admin")
        return await audit(event, **fields)

    monkeypatch.setattr(server, "audit", once)
    with caplog.at_level(logging.WARNING, logger="zab.admin.bot"):
        await harvard.on_message(incoming(CODEX_BOB, "hello", mid=3))
    assert failed == [2] and replies(site) == [FOREIGN, FOREIGN]
    assert "a foreign record failed (OSError)" in caplog.text
    assert "disk full" not in caplog.text and records(site, "foreign") == []
    await server.flush()  # not due: the merged window keeps the new one's start
    assert records(site, "foreign") == []
    site.clock.now += WINDOW
    await server.flush()
    assert _foreign(site) == [(CODEX_BOB, 3, 1, 3)] and server.foreign.open == {}


async def test_while_the_audit_log_fails_a_stranger_is_answered_once_per_window(
    site, server, monkeypatch
):
    """Codex round 1 on #579: the merge must not reset the answer window. Bob posts at 0, then
    three times at 601 s while every record fails: only the first of those is answered. A
    window later, still failing, he is answered once more; once the log works again, the next
    record counts all of his messages."""
    harvard = bot_of(server, HARVARD)
    await harvard.on_message(incoming(CODEX_BOB, "hello", mid=1))
    audit, broken = server.audit, [True]

    async def failing(event, **fields):
        if event == "foreign" and broken:
            raise OSError("disk full")
        return await audit(event, **fields)

    monkeypatch.setattr(server, "audit", failing)
    site.clock.now += WINDOW + 1
    for mid in (2, 3, 4):
        await harvard.on_message(incoming(CODEX_BOB, "hello", mid=mid))
    assert replies(site) == [FOREIGN, FOREIGN]
    site.clock.now += WINDOW
    for mid in (5, 6):
        await harvard.on_message(incoming(CODEX_BOB, "hello", mid=mid))
    assert replies(site) == [FOREIGN] * 3 and records(site, "foreign") == []
    broken.clear()
    await server.flush(every=True)
    assert _foreign(site) == [(CODEX_BOB, 6, 1, 6)]


async def test_a_record_written_before_its_append_failed_comes_twice_harmlessly(
    site, server, monkeypatch
):
    """The review of #505 (finding 2, the converse): an append that fails after its line is
    whole (`fsync`, `close`) has its window reopened, and its record is written again. As
    `flush` says, that is harmless: the two read as one window, and the chain verifies."""
    await bot_of(server, HARVARD).on_message(incoming(CODEX_BOB, "hello", mid=1))
    append, failed = server.log.append, []

    def written_then_failed(event, **fields):
        head = append(event, **fields)
        if event == "foreign" and not failed:
            failed.append(head)
            raise OSError("fsync failed")
        return head

    monkeypatch.setattr(server.log, "append", written_then_failed)
    with pytest.raises(OSError):
        await server.flush(every=True)
    await server.flush(every=True)
    assert _foreign(site) == [(CODEX_BOB, 1, 1, 1)] * 2
    assert verify(site.audit).problem is None and server.foreign.open == {}


async def test_a_window_take_closes_during_the_flush_is_written_once(site, server, monkeypatch):
    """The review of #505 (finding 1c): two windows are due. Carol posts while Bob's record
    is being written, so `take` closes her window and writes its record; the flush then
    skips it. One record each, none twice."""
    harvard = bot_of(server, HARVARD)
    await harvard.on_message(incoming(CODEX_BOB, "hello", mid=1))
    await harvard.on_message(incoming(CODEX_CAROL, "hello", mid=2))
    site.clock.now += WINDOW
    writing, go = threading.Event(), threading.Event()
    append = server.log.append

    def slow(event, **fields):
        if fields["sender"]["id"] == CODEX_BOB:
            writing.set()
            assert go.wait(5)
        return append(event, **fields)

    monkeypatch.setattr(server.log, "append", slow)
    flush = asyncio.create_task(server.flush())
    assert await asyncio.to_thread(writing.wait, 5)
    await harvard.on_message(incoming(CODEX_CAROL, "hello", mid=3))  # her window was due
    go.set()
    await flush
    assert sorted(_foreign(site)) == [(CODEX_BOB, 1, 1, 1), (CODEX_CAROL, 1, 2, 2)]
    assert server.foreign.open[(HARVARD, CODEX_CAROL)].first == 3  # her new window


async def test_a_cancelled_flush_never_reopens_the_window_it_writes(site, server, monkeypatch):
    """The review of #505 (findings 1d and 3): a flush cancelled while its append runs in its
    thread leaves the window closed. The append goes on, and is written, once."""
    await bot_of(server, HARVARD).on_message(incoming(CODEX_BOB, "hello", mid=1))
    writing, go = threading.Event(), threading.Event()
    append = server.log.append

    def slow(event, **fields):
        writing.set()
        assert go.wait(5)
        return append(event, **fields)

    monkeypatch.setattr(server.log, "append", slow)
    flush = asyncio.create_task(server.flush(every=True))
    assert await asyncio.to_thread(writing.wait, 5)
    flush.cancel()
    with pytest.raises(asyncio.CancelledError):
        await flush
    assert server.foreign.open == {}  # not reopened
    go.set()
    await until(lambda: len(_foreign(site)) == 1)


async def test_a_failed_first_register_leaves_the_user_list_to_the_next(site, server, monkeypatch):
    """The review of #505 (finding 1b): Zulip refusing the process's first register leaves
    `cross_realm_bots` unread. The next register, of any bot, fetches it, so Zulip's own bots
    are still known."""
    handle, failed = site.realm.transport.handler, []

    async def first_fails(request):
        if request.url.path.endswith("/register") and not failed:
            failed.append(True)
            return httpx.Response(401, json={"result": "error", "code": "UNAUTHORIZED"})
        return await handle(request)

    monkeypatch.setattr(site.realm.transport, "handler", first_fails)
    with pytest.raises(botmod.ZulipError):
        await bot_of(server, HARVARD).register()
    assert not server.system_bots_read and server.system_bots == set()
    await bot_of(server, TEST).register()
    assert server.system_bots == {NOTIFICATION_BOT}
    assert [fetch for _, fetch in site.realm.registers] == [["realm", "realm_user"]]


def test_the_listeners_wait_doubles_up_to_its_cap():
    assert [botmod.backoff(n) for n in range(1, 10)] == [1, 2, 4, 8, 16, 32, 64, 120, 120]
    assert botmod.backoff(10**6) == botmod.BACKOFF_CAP == 120


async def test_the_listener_backs_off_after_errors_but_a_lost_queue(site, server, monkeypatch):
    """#347 (finding 10b): a 401 (the key regenerated, the bot deactivated) or an outage is
    retried after 1 s, then 2, 4, … up to `BACKOFF_CAP`, not every second forever; at least
    what a 429 asks, and what any error's `Retry-After` asks only up to the cap (a proxy's 503
    asking an hour; the review of #505); a lost queue at once (1 s), registering a new one;
    any answer resets."""
    harvard = bot_of(server, HARVARD)
    unauthorized, down = botmod.ZulipError("UNAUTHORIZED", 401), httpx.ConnectError("down")
    script: list[object] = [unauthorized] * 8 + [
        botmod.ZulipError("BAD_EVENT_QUEUE_ID", 400),
        botmod.ZulipError("RATE_LIMIT_HIT", 429, retry_after=30.0),
        botmod.ZulipError("SERVICE_UNAVAILABLE", 503, retry_after=3600.0),
        [],  # an answer: the next failure starts again at 1 s
        *[down] * 3,  # an outage doubles too
    ]
    registered, waits = [], []

    async def register():
        registered.append(len(waits))
        return "q", 0

    async def events(queue, last):
        answer = script.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def sleep(seconds):
        waits.append(seconds)
        if not script:
            raise asyncio.CancelledError

    monkeypatch.setattr(harvard, "register", register)
    monkeypatch.setattr(harvard.client, "events", events)
    monkeypatch.setattr(botmod.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await harvard.listen()
    assert waits == [1, 2, 4, 8, 16, 32, 64, 120, 1, 30, 120, 1, 2, 4]
    assert registered == [0, 9]  # at the start, and after the lost queue


async def test_only_the_first_register_fetches_the_user_list(site, server, monkeypatch):
    """The re-review of #344, at §11's scale: `realm_user`'s state, for `cross_realm_bots`, is
    the whole user list. One register of the process fetches it, even when every bot starts
    at once, its registers in flight together; every other register, a new queue's too,
    fetches `realm` alone."""
    handle = site.realm.transport.handler

    async def slow(request):  # a register takes a moment, as a real one does
        if request.url.path.endswith("/register"):
            await asyncio.sleep(0.01)
        return await handle(request)

    monkeypatch.setattr(site.realm.transport, "handler", slow)
    await asyncio.gather(*(bot.register() for bot in server.bots))
    await bot_of(server, HARVARD).register()  # Zulip dropped its queue
    fetched = [fetch for _, fetch in site.realm.registers]
    assert sorted(map(tuple, fetched)) == [("realm",)] * 3 + [("realm", "realm_user")]
    assert server.system_bots == {NOTIFICATION_BOT}
    assert all(q.event_types == ["message", "realm_user"] for q in site.realm.queues.values())


def test_bot_check_logs_any_failure_scrubbed(site, monkeypatch, capsys):
    """#347 (the approval of #344): `bot --check` runs inside `main`'s catch-all, so an
    unexpected failure in it goes through the scrubbing handler, never `sys.excepthook`."""
    secret = (site.tmp / "etc/secrets/global.zuliprc").read_text().split("key=")[1].strip()
    server = site.server()

    async def failing(srv):
        srv.reload_secrets()
        raise RuntimeError(f"boom with {secret}")

    monkeypatch.setattr(botmod, "load_admin_config", lambda path: server.config)
    monkeypatch.setattr(botmod, "AdminServer", lambda config, config_path, **_: server)
    monkeypatch.setattr(botmod, "check", failing)
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    try:
        assert botmod.main(["--config", str(site.config_path), "--check"]) == 1
    finally:
        root.handlers[:], root.level = handlers, level
    err = capsys.readouterr().err
    assert "RuntimeError: boom with" in err and secret not in err


def test_main_logs_every_failure_scrubbed(site, monkeypatch, capsys):
    """No traceback reaches sys.excepthook: it goes through the scrubbing handler."""
    secret = (site.tmp / "etc/secrets/global.zuliprc").read_text().split("key=")[1].strip()
    server = site.server()

    async def failing(srv):
        srv.reload_secrets()
        raise RuntimeError(f"boom with {secret}")

    monkeypatch.setattr(botmod, "load_admin_config", lambda path: server.config)
    monkeypatch.setattr(botmod, "AdminServer", lambda config, config_path, **_: server)
    monkeypatch.setattr(botmod, "serve", failing)
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    try:
        assert botmod.main(["--config", str(site.config_path)]) == 1
    finally:
        root.handlers[:], root.level = handlers, level
    err = capsys.readouterr().err
    assert "stopped by an error" in err and "RuntimeError: boom with" in err
    assert secret not in err
