"""Scenario 16's inputs and readers (#709), offline, against the product itself: each typed form
it sends reads, through `zab.core.command_text` (and the sentinel's `command_text`), as the
command it stands for, and its prompt as no command; its reply patterns match the bridge's
replies as the code builds them; its sign-in reader reads `zab doctor --json`."""

from __future__ import annotations

import json
import re

import pytest

from e2e import test_scenarios_typed as s16
from e2e.harness import installed, report
from zab.backends.claude_login import INSTRUCTIONS
from zab.backends.codex import device_code_reply
from zab.core.addressing import mention
from zab.core.command_text import as_command
from zab.core.commands import DM_HELP, HELP
from zab.core.router import Router
from zab.core.store import Store
from zab.tools import sentinel

NAMES = {"claude": "Claude · zab-test", "codex": "Codex · zab-test"}
IDS = {"claude": 301, "codex": 302}


def test_scenario_16_is_registered_fast():
    assert report.SCENARIOS[16] == ("Commands as people type them (#709)", "fast")
    assert "zab.core.command_text" in installed.HAS_COMMAND_TEXT


def dm_route(tmp_path, kind: str, text: str) -> str:
    """What the bot of `kind` (named `NAMES[kind]`, the scenario's names) reads in a one-to-one
    DM `text`: its router's DM route, which composes #713's typed address and #709's forms."""
    store = Store(tmp_path / kind)
    try:
        router = Router(store, kind, IDS[kind], NAMES[kind], frozenset({20}), kind=kind)
        dm = dict(id=1, sender_id=20, content=text, type="private")
        return router.route(dm, flags=[], sender_is_bot=False, private=True).text
    finally:
        store.close()


def test_each_form_sent_reads_as_its_command(tmp_path):
    """As the bridge's router and the sentinel read them, with the bot's kind and mention."""
    claude = mention(NAMES["claude"], IDS["claude"])
    for i, text in enumerate(s16.vm_forms(NAMES["claude"])):
        assert dm_route(tmp_path / f"vm{i}", "claude", text) == "!vm", text
        assert sentinel.command_text(text, "claude", claude) == "!vm", text  # so the bridge
        # leaves it to the sentinel
    for i, (kind, text, _) in enumerate(s16.bridge_forms(NAMES["claude"])):
        assert dm_route(tmp_path / f"b{i}", kind, text) in ("!help", "!status"), text
        bot = mention(NAMES[kind], IDS[kind])
        assert sentinel.command_text(text, kind, bot) is None, text  # the bridge's alone
    for kind in ("claude", "codex"):
        for i, (start, cancel) in enumerate(s16.login_forms(NAMES[kind], kind)):
            assert dm_route(tmp_path / f"l{kind}{i}", kind, start) == "!login", start
            read = dm_route(tmp_path / f"c{kind}{i}", kind, cancel)
            assert [w.casefold() for w in read.split()] == ["!login", "cancel"], cancel
    assert as_command("`!help`") == "!help" and as_command("!Usage") == "!usage"
    text = s16.prompt("TOKEN")
    assert as_command(text) == text and "TOKEN" in text  # delivered to the agent as it is
    assert s16.typed("claude", text) == f"@claude {text}"


def test_the_reply_patterns_match_the_products_replies():
    assert re.search(s16.topic_help(), HELP) and re.search(
        s16.bridge_forms(NAMES["claude"])[0][2], DM_HELP
    )
    codex = device_code_reply("https://auth.openai.com/codex/device", "ABCD-12345")
    assert re.search(s16.codex_reply(), codex)
    old = (
        "First, in ChatGPT → Settings → Security, turn on Allow device code login. Then open "
        "https://auth.openai.com/codex/device and enter ABCD-12345."
    )
    assert not re.search(s16.codex_reply(), old)  # 0.2.15's
    claude = f"{INSTRUCTIONS}\nhttps://claude.com/cai/oauth/authorize?code=true&client_id=x"
    assert re.search(s16.claude_reply(), claude)
    assert not re.search(s16.claude_reply(), "Claude did not show a sign-in link; try again.")
    assert re.fullmatch(s16.CANCELLED, "❌ Login failed or cancelled.")


@pytest.mark.parametrize(
    "checks, found",
    [
        (
            [{"name": "codex:login", "ok": True}, {"name": "claude account default", "ok": True}],
            {"claude": True, "codex": True},
        ),
        (
            [
                {"name": "codex:login", "ok": False},
                {"name": "claude account default", "ok": True},
                {"name": "claude account work", "ok": False},
            ],
            {"claude": False, "codex": False},
        ),
        ([{"name": "claude auth default", "ok": True}], {"claude": None, "codex": None}),
    ],
)
def test_sign_ins_reads_the_doctor(checks, found):
    assert s16.sign_ins(json.dumps(checks)) == found


def test_sign_ins_of_no_doctor_output_is_unknown():
    for stdout in ("", "Traceback …", "{}", "[1, 2]"):
        assert s16.sign_ins(stdout) == {"claude": None, "codex": None}
